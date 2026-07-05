from flask import Flask, jsonify, render_template
import cv2
import requests
import threading
import time
import logging
from datetime import datetime
import board
import busio
import adafruit_dht
from collections import deque
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json
import RPi.GPIO as GPIO

# Initialize Flask app
app = Flask(__name__)

# ----------------- DIGITAL SENSOR CONFIG -----------------
# GPIO Pin Configuration
SOIL_MOISTURE_PIN = 17    # Digital soil moisture sensor
RAINFALL_SENSOR_PIN = 27  # Digital rainfall sensor

# DHT11 Sensor (Temperature/Humidity)
try:
    DHT_SENSOR = adafruit_dht.DHT11(board.D4, use_pulseio=False)
    print("DHT11 sensor initialized successfully")
except Exception as e:
    logging.error(f"DHT11 initialization failed: {e}")
    DHT_SENSOR = None

# GPIO Setup
try:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(SOIL_MOISTURE_PIN, GPIO.IN)
    GPIO.setup(RAINFALL_SENSOR_PIN, GPIO.IN)
    print("GPIO pins initialized successfully")
except Exception as e:
    logging.error(f"GPIO initialization failed: {e}")

# ----------------- CAMERA CONFIG -----------------
# Fixed endpoint - using image endpoint, not URL endpoint
PREDICTION_URL = "https://plasticbottledetection-prediction.cognitiveservices.azure.com/customvision/v3.0/Prediction/7262f95a-0833-4c84-b492-47be8b20ea99/detect/iterations/Iteration5/image"
PREDICTION_KEY = "3qqW7U6DcR0JreFST9e9N09MrS1pJnj7Dj3WmwUFn1vY8mZ2JRP9JQQJ99BIACqBBLyXJ3w3AAAIACOGVI4W"
CONFIDENCE_THRESHOLD = 0.70  # Lowered for testing
CAMERA_ID = 0
FRAME_WIDTH = 640
FRAME_SKIP = 30  # Increased to reduce API calls
REQUEST_TIMEOUT = 10

# ----------------- EMAIL CONFIG -----------------
EMAIL_SENDER = "pestdetectionsystem@gmail.com"
EMAIL_PASSWORD = "tskxhonpyrkdtpic"
EMAIL_RECEIVER = "venkatathanush725@gmail.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

TEMP_ALERT_THRESHOLD = 35
RAINFALL_ALERT_COUNT = 5  # Number of rainfall detections to trigger alert

# ----------------- GLOBAL VARIABLES -----------------
sensor_data = {
    'temperature': 0,
    'humidity': 0,
    'soil_moisture': "Dry",  # Digital: Dry/Wet
    'rainfall': "No Rain",   # Digital: No Rain/Raining
    'soil_moisture_raw': 0,
    'rainfall_raw': 0,
    'last_updated': datetime.now().isoformat()
}

latest_predictions = []
detected_pests = deque(maxlen=10)
pred_lock = threading.Lock()
sensor_lock = threading.Lock()

last_alert_time = {
    'temperature': datetime.min,
    'rainfall': datetime.min,
    'pest': datetime.min
}
ALERT_COOLDOWN = 3600  # 1 hour

# Rainfall detection variables
rainfall_detection_count = 0
RAINFALL_SAMPLE_WINDOW = 10  # Number of samples to confirm rainfall

# Azure session with correct headers
session = requests.Session()
session.headers.update({
    "Prediction-Key": PREDICTION_KEY,
    "Content-Type": "application/octet-stream"
})

# ----------------- EMAIL FUNCTIONS -----------------
def send_email_alert(subject, message):
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        msg['Subject'] = subject
        msg.attach(MIMEText(message, 'plain'))
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        server.quit()
        
        logging.info(f"Email alert sent: {subject}")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False

# ----------------- DIGITAL SENSOR FUNCTIONS -----------------
def read_dht_sensor():
    if DHT_SENSOR is None:
        return None, None
    try:
        temperature = DHT_SENSOR.temperature
        humidity = DHT_SENSOR.humidity
        if temperature is not None and humidity is not None:
            print(f"DHT11 - Temp: {temperature}°C, Humidity: {humidity}%")
        return temperature, humidity
    except RuntimeError as error:
        logging.warning(f"DHT11 read error: {error}")
        return None, None
    except Exception as error:
        logging.error(f"DHT11 unexpected error: {error}")
        return None, None

def read_digital_soil_moisture():
    """Read digital soil moisture sensor (0 = Wet, 1 = Dry)"""
    try:
        # Digital soil moisture sensor typically:
        # LOW (0) = Wet/Water detected
        # HIGH (1) = Dry/No water
        sensor_value = GPIO.input(SOIL_MOISTURE_PIN)
        
        if sensor_value == GPIO.LOW:
            status = "Wet"
            moisture_level = 100  # 100% when wet
        else:
            status = "Dry" 
            moisture_level = 0    # 0% when dry
            
        print(f"Soil Moisture - Digital: {sensor_value}, Status: {status}")
        return status, moisture_level, sensor_value
        
    except Exception as error:
        logging.error(f"Soil moisture sensor error: {error}")
        return "Error", 0, -1

def read_digital_rainfall():
    """Read digital rainfall sensor with debouncing"""
    global rainfall_detection_count
    
    try:
        # Digital rainfall sensor typically:
        # LOW (0) = Rain detected
        # HIGH (1) = No rain
        sensor_value = GPIO.input(RAINFALL_SENSOR_PIN)
        
        # Debouncing and confirmation logic
        if sensor_value == GPIO.LOW:
            rainfall_detection_count = min(rainfall_detection_count + 1, RAINFALL_SAMPLE_WINDOW)
        else:
            rainfall_detection_count = max(rainfall_detection_count - 1, 0)
        
        # Determine rainfall status
        if rainfall_detection_count >= RAINFALL_ALERT_COUNT:
            status = "Raining"
            intensity = 10  # Maximum intensity when confirmed raining
        elif rainfall_detection_count > 0:
            status = "Light Rain"
            intensity = rainfall_detection_count  # Scale intensity with detection count
        else:
            status = "No Rain"
            intensity = 0
            
        print(f"Rainfall - Digital: {sensor_value}, Count: {rainfall_detection_count}, Status: {status}")
        return status, intensity, sensor_value
        
    except Exception as error:
        logging.error(f"Rainfall sensor error: {error}")
        return "Error", 0, -1

def update_sensor_data():
    global sensor_data, last_alert_time
    
    # Read all sensors
    temperature, humidity = read_dht_sensor()
    soil_status, soil_moisture, soil_raw = read_digital_soil_moisture()
    rain_status, rain_intensity, rain_raw = read_digital_rainfall()
    
    with sensor_lock:
        # Update temperature and humidity
        if temperature is not None:
            if temperature > TEMP_ALERT_THRESHOLD and (datetime.now() - last_alert_time['temperature']).total_seconds() > ALERT_COOLDOWN:
                subject = f"High Temperature Alert: {temperature}°C"
                message = f"Temperature exceeded threshold: {temperature}°C"
                if send_email_alert(subject, message):
                    last_alert_time['temperature'] = datetime.now()
                    print(f"Temperature alert sent: {temperature}°C")
            sensor_data['temperature'] = temperature
        
        if humidity is not None:
            sensor_data['humidity'] = humidity
        
        # Update soil moisture data
        sensor_data['soil_moisture'] = soil_status
        sensor_data['soil_moisture_raw'] = soil_raw
        
        # Update rainfall data and check for alerts
        sensor_data['rainfall'] = rain_status
        sensor_data['rainfall_raw'] = rain_raw
        
        if rain_status == "Raining" and (datetime.now() - last_alert_time['rainfall']).total_seconds() > ALERT_COOLDOWN:
            subject = f"Rainfall Alert: {rain_status}"
            message = f"Rainfall detected with status: {rain_status}"
            if send_email_alert(subject, message):
                last_alert_time['rainfall'] = datetime.now()
                print(f"Rainfall alert sent: {rain_status}")
        
        sensor_data['last_updated'] = datetime.now().isoformat()
        
        print(f"Sensor data updated: {sensor_data}")

# ----------------- CAMERA FUNCTIONS -----------------
def send_frame_for_detection(jpeg_bytes):
    global latest_predictions, detected_pests, last_alert_time
    try:
        print(f"Sending frame for detection, size: {len(jpeg_bytes)} bytes")
        
        resp = session.post(PREDICTION_URL, data=jpeg_bytes, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        
        print(f"Detection API response received")
        
        preds = []
        current_time = datetime.now().isoformat()
        detection_found = False
        
        for p in data.get("predictions", []):
            probability = p.get("probability", 0)
            tag_name = p.get("tagName", "")
            
            if probability >= CONFIDENCE_THRESHOLD:
                box = p.get("boundingBox", {})
                pred_data = {
                    "tagName": tag_name,
                    "probability": float(probability),
                    "left": float(box.get("left", 0.0)),
                    "top": float(box.get("top", 0.0)),
                    "width": float(box.get("width", 0.0)),
                    "height": float(box.get("height", 0.0)),
                    "timestamp": current_time
                }
                preds.append(pred_data)
                
                detection = {
                    "name": tag_name,
                    "confidence": float(probability),
                    "timestamp": current_time
                }
                detected_pests.append(detection)
                detection_found = True
                
                print(f"✅ Detection: {tag_name} with {probability*100:.1f}% confidence")
                
                if (datetime.now() - last_alert_time['pest']).total_seconds() > ALERT_COOLDOWN:
                    subject = f"Pest Detected: {tag_name}"
                    message = f"A {tag_name} was detected with {probability*100:.1f}% confidence."
                    if send_email_alert(subject, message):
                        last_alert_time['pest'] = datetime.now()
                        print(f"Pest alert email sent: {tag_name}")
        
        if not detection_found:
            print("No objects detected above confidence threshold")
            
        with pred_lock:
            latest_predictions = preds
            
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            logging.warning("Rate limited by Azure API, slowing down requests")
        else:
            logging.error(f"Detection HTTP error {e.response.status_code}: {e.response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Detection request failed: {e}")
    except Exception as e:
        logging.error(f"Unexpected error in detection: {e}")

def camera_processing():
    print("Starting camera processing...")
    
    # Try different camera IDs if default doesn't work
    camera_ids = [0, 1, 2]
    cap = None
    
    for camera_id in camera_ids:
        cap = cv2.VideoCapture(camera_id)
        if cap.isOpened():
            print(f"Camera found at ID: {camera_id}")
            break
        else:
            print(f"Camera not found at ID: {camera_id}")
    
    if not cap or not cap.isOpened():
        logging.error("Unable to open any camera")
        return

    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        frame_count = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                logging.error("Failed to read frame from camera")
                time.sleep(2)
                continue

            frame_count += 1
            
            # Resize frame if too large
            h, w = frame.shape[:2]
            if w > FRAME_WIDTH:
                new_h = int(FRAME_WIDTH * h / w)
                frame_small = cv2.resize(frame, (FRAME_WIDTH, new_h))
            else:
                frame_small = frame.copy()

            # Send frame for detection periodically
            if (frame_count % FRAME_SKIP) == 0:
                success, jpg = cv2.imencode('.jpg', frame_small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if success:
                    thread = threading.Thread(target=send_frame_for_detection, args=(jpg.tobytes(),))
                    thread.daemon = True
                    thread.start()
                else:
                    logging.error("Failed to encode frame as JPEG")

            time.sleep(0.1)  # Small delay to prevent excessive CPU usage
            
    except KeyboardInterrupt:
        logging.info("Camera processing stopped by user")
    except Exception as e:
        logging.error(f"Camera processing error: {e}")
    finally:
        if cap:
            cap.release()
        print("Camera released")

# ----------------- FLASK ROUTES -----------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/sensor-data')
def get_sensor_data():
    with sensor_lock:
        return jsonify(sensor_data)

@app.route('/api/pest-detections')
def get_pest_detections():
    with pred_lock:
        # Get unique pests (latest detection for each type)
        unique_pests = {}
        for pest in detected_pests:
            unique_pests[pest['name']] = pest
        return jsonify(list(unique_pests.values()))

@app.route('/api/update-sensors')
def update_sensors():
    update_sensor_data()
    with sensor_lock:
        return jsonify(sensor_data)

@app.route('/api/system-status')
def get_system_status():
    status = {
        'camera_connected': True,
        'sensors_connected': DHT_SENSOR is not None,
        'digital_sensors_working': True,
        'api_connected': True,
        'last_camera_frame': datetime.now().isoformat()
    }
    return jsonify(status)

# ----------------- CLEANUP FUNCTION -----------------
def cleanup():
    """Cleanup GPIO resources"""
    try:
        GPIO.cleanup()
        print("GPIO cleanup completed")
    except Exception as e:
        logging.error(f"Error during GPIO cleanup: {e}")

# ----------------- MAIN EXECUTION -----------------
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    print("🚀 Starting Pest Detection System with Digital Sensors...")
    print("📊 Sensors: Temperature, Humidity, Digital Soil Moisture, Digital Rainfall")
    print("📷 Camera: Pest detection via Azure Custom Vision")
    print("📧 Alerts: Email notifications for thresholds")
    print("🌐 Web Interface: http://localhost:5000")

    # Start camera thread
    camera_thread = threading.Thread(target=camera_processing)
    camera_thread.daemon = True
    camera_thread.start()
    print("📷 Camera thread started")

    # Sensor update loop
    def sensor_update_loop():
        print("📊 Digital sensor monitoring started")
        while True:
            update_sensor_data()
            time.sleep(5)  # Update more frequently for digital sensors

    sensor_thread = threading.Thread(target=sensor_update_loop)
    sensor_thread.daemon = True
    sensor_thread.start()
    print("📊 Sensor thread started")

    print("✅ System initialized successfully!")
    print("🔍 Access the dashboard at: http://localhost:5000")
    
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\nShutting down system...")
    finally:
        cleanup()
