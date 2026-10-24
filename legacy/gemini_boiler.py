#!/usr/bin/python3.11

import base64
import google.generativeai as genai
import sys

API_KEY = ""

genai.configure(api_key=API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')


def encode_image(image_path):
    """Encodes an image file to base64."""
    with open(image_path, "rb") as image_file:
        encoded_image = base64.b64encode(image_file.read()).decode("utf-8")
    return encoded_image


if len(sys.argv) < 2:
    print("Usage: python your_script_name.py <path_to_image>")
    sys.exit(1)

IMAGE_PATH = sys.argv[1]

try:
    image_data = encode_image(IMAGE_PATH)
    contents = [
        {"mime_type": "image/jpeg", "data": image_data},
        "What are the temperature values (including the unit) visible in this image? Return only the temperature values and the unit. Separate the values with empty line.",
    ]
    response = model.generate_content(contents)
    print(response.text)

except FileNotFoundError:
    print(f"Error: Image file not found at '{IMAGE_PATH}'")
except Exception as e:
    print(f"An error occurred: {e}")
    print("Make sure your API key is correct and the model is available.")
