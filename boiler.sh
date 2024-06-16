#!/bin/bash

# Configuration variables
REMOTE_USER=""
REMOTE_HOST=""
WEB_SERVER=""
WEB_DIR="/var/www/html/share"
INFLUXDB_URL="http://localhost:8086/write?db=boiler"
TEMP_FILE="/tmp/previous_temps.txt"
TEMP_THRESHOLD=30  # Temperature difference threshold to detect unrealistic changes


# Function to check if the tesseract output contains exactly one temperature
check_output() {
    local output="$1"
    local temp_count=$(echo "$output" | grep -o '[0-9]\+°C' | wc -l)
    if [ "$temp_count" -eq 1 ]; then
        return 0  # True if there is one temperature
    else
        return 1  # False otherwise
    fi
}

# Function to process temperature extraction
process_temperature() {
    local label="$1"
    local crop_params="$2"
    local img_output="$3"
    local temp_var_name="$4"

    for stretch_min in {1..10}; do
        for stretch_max in {10..80..5}; do
            echo "Trying with contrast-stretch ${stretch_min}x${stretch_max}% for ${label} temperature"
            convert -density 300 -depth 8 -resize 1600x1200 ${crop_params} -contrast-stretch ${stretch_min}x${stretch_max}% -colorspace Gray /tmp/boiler_screen.jpg ${img_output}
            
            # Recognize text using tesseract
            tesseract_output=$(tesseract ${img_output} stdout 2>/dev/null)
            tesseract_output="${tesseract_output//S/5}"
            tesseract_output="${tesseract_output//s/5}"
            tesseract_output="${tesseract_output//$/5}"
            tesseract_output="${tesseract_output//€/6}"

            if check_output "$tesseract_output"; then
                echo "Success with contrast-stretch ${stretch_min}x${stretch_max}%"
                echo "Tesseract output for ${label}:"
                echo "$tesseract_output"

                local temp=$(echo "$tesseract_output" | grep -o '[0-9]\+°C' | sed -n '1p')
                eval ${temp_var_name}=${temp//[^0-9]/}
                return 0
            fi
        done
    done

    return 1
}

# Capture image from remote device
ssh ${REMOTE_USER}@${REMOTE_HOST} -t "fswebcam --set contrast=70% -D 2 --set brightness=20% -F 10 -r 640x480 -S 30 -d /dev/video0 boiler_screen.jpg" 
if [ $? -ne 0 ]; then
    echo "Error capturing image from remote device."
    exit 1
fi

scp ${REMOTE_USER}@${REMOTE_HOST}:boiler_screen.jpg /tmp/
if [ $? -ne 0 ]; then
    echo "Error copying image from remote device."
    exit 1
fi

roof_temp_number=0
boiler_temp_number=0

# Process roof temperature
process_temperature "roof" "-crop -398-750 -crop +980+340" "/tmp/roof_value.jpg" "roof_temp_number"
roof_temp=$roof_temp_number

# Process boiler temperature
process_temperature "boiler" "-crop -398-590 -crop +980+450" "/tmp/boiler_value.jpg" "boiler_temp_number"
boiler_temp=$boiler_temp_number

# Check previous temperatures if file exists
if [ -f "$TEMP_FILE" ]; then
    read -r prev_roof_temp prev_boiler_temp < "$TEMP_FILE"
    if [ $((roof_temp_number - prev_roof_temp)) -gt $TEMP_THRESHOLD ] || [ $((prev_roof_temp - roof_temp_number)) -gt $TEMP_THRESHOLD ]; then
        echo "Unrealistic change detected in roof temperature: $prev_roof_temp -> $roof_temp_number"
        roof_temp_number=$prev_roof_temp
        roof_temp="${roof_temp_number}°C"
    fi
    if [ $((boiler_temp_number - prev_boiler_temp)) -gt $TEMP_THRESHOLD ] || [ $((prev_boiler_temp - boiler_temp_number)) -gt $TEMP_THRESHOLD ]; then
        echo "Unrealistic change detected in boiler temperature: $prev_boiler_temp -> $boiler_temp_number"
        boiler_temp_number=$prev_boiler_temp
        boiler_temp="${boiler_temp_number}°C"
    fi
fi

# Store current temperatures for the next execution
echo "$roof_temp_number $boiler_temp_number" > "$TEMP_FILE"


# Generate HTML file with the extracted number
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
        <img src="roof_value.jpg" alt="Processed Image">
        <img src="boiler_value.jpg" alt="Processed Image">
    </div>
</body>
</html>
EOF

scp /tmp/bojler.html ${WEB_SERVER}:${WEB_DIR}/
scp /tmp/boiler_screen.jpg ${WEB_SERVER}:${WEB_DIR}/
scp /tmp/roof_value.jpg ${WEB_SERVER}:${WEB_DIR}/
scp /tmp/boiler_value.jpg ${WEB_SERVER}:${WEB_DIR}/

if [ "$roof_temp_number" -eq 0 ] || [ "$boiler_temp_number" -eq 0 ]; then
    echo "No suitable contrast-stretch values found to recognize two temperatures."
    exit 1
fi

temp_data="temperatures roof=${roof_temp_number},boiler=${boiler_temp_number}"
echo $temp_data

curl -i -XPOST ${INFLUXDB_URL} --data-binary "${temp_data}"

exit 0

