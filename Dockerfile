# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install FFmpeg (essential for audio) and system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy the dependencies file to the working directory
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# --no-cache-dir reduces image size
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code to the working directory
COPY . .

# Define the environment variable for the bot token (will be passed during 'docker run')
ENV DISCORD_TOKEN=YOUR_TOKEN_GOES_HERE_BUT_PASS_IT_VIA_RUN_COMMAND

# Command to run the bot when the container launches
CMD ["python", "bot.py"]