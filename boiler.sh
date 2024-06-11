#!/bin/bash

# Variables
REMOTE_IP=""
REMOTE_USER=""
REMOTE_IMAGE_PATH="/tmp/boiler_screen.jpg"
LOCAL_IMAGE_PATH="/tmp/boiler_screen.jpg"
LOCAL_PROCESSED_IMAGE_PATH="/tmp/boiler_values.jpg"
OUTPUT_HTML_PATH="bojler.html"
UPLOAD_SERVER=""
UPLOAD_PATH=""

# Function to check if the tesseract output contains exactly two temperatures
check_output() {
    local output="$1"
    local temp_count=$(echo "$output" | grep -o '[0-9]\+°C' | wc -l)
    if [ "$temp_count" -eq 2 ]; then
        return 0  # True if there are exactly two temperatures
    else
        return 1  # False otherwise
    fi
}

# Capture image from remote device
ssh ${REMOTE_USER}@${REMOTE_IP} -t "fswebcam --set contrast=70% -D 2 --set brightness=20% -F 10 -r 640x480 -S 30 -d /dev/video0 ${REMOTE_IMAGE_PATH}" 
scp ${REMOTE_USER}@${REMOTE_IP}:${REMOTE_IMAGE_PATH} /tmp/ 

# Loop through different values of contrast-stretch
for stretch_min in {1..10}; do
    for stretch_max in {30..70..5}; do
        # Convert the image with the current contrast-stretch parameters
        echo "Trying with contrast-stretch ${stretch_min}x${stretch_max}%"
        convert -rotate -8 -density 300 -depth 8 -resize 1600x1200 -crop -658-640 -crop +660+380 +repage -contrast-stretch ${stretch_min}x${stretch_max}% -colorspace Gray ${LOCAL_IMAGE_PATH} ${LOCAL_PROCESSED_IMAGE_PATH}
        
        # Recognize text using tesseract
        tesseract_output=$(tesseract ${LOCAL_PROCESSED_IMAGE_PATH} stdout 2>/dev/null)
        
        # Check if the output contains exactly two temperatures
        if check_output "$tesseract_output"; then
            echo "Success with contrast-stretch ${stretch_min}x${stretch_max}%"
            echo "Tesseract output:"
            tesseract_output="${tesseract_output//S/5}"
            echo "$tesseract_output"
            
            # Read the extracted text
            roof_temp=$(echo "$tesseract_output" | grep -o '[0-9]\+°C' | sed -n '1p')
            boiler_temp=$(echo "$tesseract_output" | grep -o '[0-9]\+°C' | sed -n '2p')
            
            # Generate HTML file with the extracted numbers
            cat <<EOF > ${OUTPUT_HTML_PATH}
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
        <img src="boiler_values.jpg" alt="Processed Image">
    </div>
</body>
</html>
EOF

            # Upload files to server
            scp ${OUTPUT_HTML_PATH} ${UPLOAD_SERVER}:${UPLOAD_PATH}
            scp ${LOCAL_IMAGE_PATH} ${UPLOAD_SERVER}:${UPLOAD_PATH}
            scp ${LOCAL_PROCESSED_IMAGE_PATH} ${UPLOAD_SERVER}:${UPLOAD_PATH}

            exit 0
        fi
    done
done

echo "No suitable contrast-stretch values found to recognize two temperatures."
exit 1

