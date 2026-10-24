#!/bin/bash

# Configuration variables
REMOTE_USER=""
REMOTE_HOST=""
WEB_SERVER=""
WEB_DIR="/var/www/html/share"
INFLUXDB_URL="http://localhost:8086/write?db=boiler"
TEMP_FILE="/tmp/previous_temps.txt"
LOG_FILE="/tmp/boiler-ai.log"
TEMP_THRESHOLD=30  # Temperature difference threshold to detect unrealistic changes
MQTT_HOST=""    #  Home Assistant broker IP
MQTT_TOPIC="home/boiler"


# Logging function
log_msg() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Function to calculate absolute value for difference check
abs() {
    if [ $1 -lt 0 ]; then
        echo $((-$1))
    else
        echo $1
    fi
}

# Capture image from remote device
ssh ${REMOTE_USER}@${REMOTE_HOST} -t "fswebcam --set contrast=70% -D 2 --set brightness=25% -F 10 -r 640x480 -S 30 -d /dev/video0 boiler_screen.jpg"
if [ $? -ne 0 ]; then
    log_msg "Error capturing image from remote device."
    exit 1
fi

scp ${REMOTE_USER}@${REMOTE_HOST}:boiler_screen.jpg /tmp/
if [ $? -ne 0 ]; then
    log_msg "Error copying image from remote device."
    exit 1
fi

roof_temp_number=0
boiler_temp_number=0

# Process temperatures from Gemini
temps=$(gemini_boiler.py /tmp/boiler_screen.jpg)

roof_temp=$(echo $temps | awk '{print $1}')
roof_temp_number=${roof_temp//[^0-9-]/} # <-- MODIFIED: Allows digits and hyphen
log_msg "Roof temp raw: $roof_temp (parsed: $roof_temp_number)"

boiler_temp=$(echo $temps | awk '{print $2}')
boiler_temp_number=${boiler_temp//[^0-9-]/} # <-- MODIFIED: Allows digits and hyphen
log_msg "Boiler temp raw: $boiler_temp (parsed: $boiler_temp_number)"

# Check previous temperatures if file exists
if [ -f "$TEMP_FILE" ]; then
    read -r prev_roof_temp prev_boiler_temp < "$TEMP_FILE"

    # Validate that both previous temps are numbers (allows optional leading minus)
    if [[ ! "$prev_roof_temp" =~ ^-?[0-9]+$ ]] || [[ ! "$prev_boiler_temp" =~ ^-?[0-9]+$ ]]; then # <-- MODIFIED
        log_msg "Corrupted temp file detected, resetting with current values."
        prev_roof_temp=$roof_temp_number
        prev_boiler_temp=$boiler_temp_number
    else
        # Calculate absolute difference between current and previous readings
        roof_diff=$(abs $((roof_temp_number - prev_roof_temp)))
        boiler_diff=$(abs $((boiler_temp_number - prev_boiler_temp)))
        
        # Check unrealistic changes
        if [ $roof_diff -gt $TEMP_THRESHOLD ]; then # <-- MODIFIED: Use absolute difference
            log_msg "Unrealistic change in roof temperature: $prev_roof_temp -> $roof_temp_number (keeping previous)"
            roof_temp_number=$prev_roof_temp
            roof_temp="${roof_temp_number}°C"
        fi

        if [ $boiler_diff -gt $TEMP_THRESHOLD ]; then # <-- MODIFIED: Use absolute difference
            log_msg "Unrealistic change in boiler temperature: $prev_boiler_temp -> $boiler_temp_number (keeping previous)"
            boiler_temp_number=$prev_boiler_temp
            boiler_temp="${boiler_temp_number}°C"
        fi
    fi
else
    log_msg "No previous temp file found, creating new one."
fi

# Store current temperatures for the next execution (self-heal always)
echo "$roof_temp_number $boiler_temp_number" > "$TEMP_FILE"

# Generate HTML file with the extracted numbers
cat <<EOF > /tmp/bojler.html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Temperature Readings</title>
    <style>
        body {
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background-color: #f0f0f0;
            flex-direction: column;
        }
        .temperature {
            font-size: 5em;
            font-weight: bold;
            color: #4CAF50;
            margin: 20px;
            text-align: center;
        }
        .image-container {
            display: flex;
            justify-content: center;
            flex-wrap: wrap;
        }
        .image-container img {
            max-width: 100%;
            max-height: 100%;
            height: auto;
            margin: 10px;
            object-fit: contain;
        }
        @media (max-width: 600px) {
            .temperature {
                font-size: 2.5em;
                margin: 10px;
            }
            .image-container {
                flex-direction: column;
                align-items: center;
            }
        }
    </style>
</head>
<body>
    <div class="temperature">Roof Temperature: $roof_temp</div>
    <div class="temperature">Boiler Temperature: $boiler_temp</div>
    <div class="image-container">
        <img src="boiler_screen.jpg" alt="Original Image">
    </div>
</body>
</html>
EOF

scp /tmp/bojler.html ${WEB_SERVER}:${WEB_DIR}/
scp /tmp/boiler_screen.jpg ${WEB_SERVER}:${WEB_DIR}/

temp_data="temperatures roof=${roof_temp_number},boiler=${boiler_temp_number}"
log_msg "Sending data to InfluxDB: $temp_data"
curl -s -i -XPOST ${INFLUXDB_URL} --data-binary "${temp_data}" >> "$LOG_FILE" 2>&1
/usr/bin/mosquitto_pub -h $MQTT_HOST -t "$MQTT_TOPIC/roof" -m "$roof_temp_number"
/usr/bin/mosquitto_pub -h $MQTT_HOST -t "$MQTT_TOPIC/boiler" -m "$boiler_temp_number"

log_msg "Run finished successfully."
exit 0
