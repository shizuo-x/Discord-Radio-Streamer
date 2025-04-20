# Discord Radio Bot (Dockerized)

A simple Discord bot focused on playing online radio streams, packaged for easy deployment using Docker and Docker Compose.

## Features

*   Plays online radio stream URLs (HTTP/HTTPS).
*   Supports predefined radio stream names (configurable in `bot.py`).
*   Control playback with Prefix Commands (default: `,,`) and Slash Commands (`/`).
*   Displays a "Now Playing" embed with stream info.
*   Stop playback using commands or reacting with ⏹️ to the Now Playing message.
*   Automatic reconnection attempts on stream errors.

## Prerequisites

*   **Docker:** Install Docker Desktop (Windows, macOS) or Docker Engine (Linux). Get it from [https://www.docker.com/get-started](https://www.docker.com/get-started).
*   **Docker Compose:** Usually included with Docker Desktop. For Linux, you might need to install it separately (follow Docker's official documentation).

## Setup Instructions

1.  **Get the Bot Files:**
    *   Download or clone this repository/folder to your computer.

2.  **Navigate to the Folder:**
    *   Open a terminal or command prompt and change directory into the `discord-radio-bot-public` folder (the one containing this `README.md`).
    ```bash
    cd path/to/discord-radio-bot-public
    ```

3.  **Create the `.env` File:**
    *   Find the file named `.env.example` in this folder.
    *   Make a **copy** of this file and rename the copy to `.env`.
    *   **Linux/macOS:** `cp .env.example .env`
    *   **Windows (Command Prompt):** `copy .env.example .env`
    *   **Windows (PowerShell):** `Copy-Item .env.example .env`

4.  **Get Your Discord Bot Token:**
    *   Go to the [Discord Developer Portal](https://discord.com/developers/applications).
    *   Click **"New Application"** (or select an existing one). Give it a name (e.g., "My Radio Bot").
    *   Navigate to the **"Bot"** tab on the left menu.
    *   Click **"Add Bot"** and confirm.
    *   Under the bot's username, find the **"Token"** section. Click **"Reset Token"** (or "View Token") and copy the token string. **Treat this token like a password!**
    *   **Crucially:** Scroll down on the Bot page to **"Privileged Gateway Intents"**. Enable **BOTH**:
        *   `SERVER MEMBERS INTENT`
        *   `MESSAGE CONTENT INTENT`
    *   Save changes if prompted.

5.  **Configure `.env`:**
    *   Open the `.env` file you created in Step 3 with a text editor.
    *   Replace the placeholder `YOUR_BOT_TOKEN_GOES_HERE` with the actual bot token you copied from the Discord Developer Portal.
    *   The line should look like: `DISCORD_TOKEN=AbCdEfGhIjKlMnOpQrStUvWxYz.aBcDeF.abcdefghijklmnopqrstuvwxyz123456` (but with your real token).
    *   Save and close the `.env` file.

6.  **Build and Run the Bot:**
    *   Make sure Docker Desktop or Docker Engine is running.
    *   In your terminal (still inside the `discord-radio-bot-public` folder), run the following command:
    ```bash
    docker-compose up -d
    ```
    *   **What this does:**
        *   `docker-compose`: Invokes the Docker Compose tool.
        *   `up`: Builds the Docker image (if it doesn't exist) using the `Dockerfile`, creates a container based on the `docker-compose.yml` service definition, and starts the container.
        *   `-d`: (Detached mode) Runs the container in the background, so you can close the terminal.

7.  **Invite Your Bot:**
    *   Go back to the Discord Developer Portal and your bot application.
    *   Navigate to **OAuth2 -> URL Generator**.
    *   Select the following scopes:
        *   `bot`
        *   `applications.commands`
    *   In the "Bot Permissions" section that appears, select:
        *   `Send Messages`
        *   `Embed Links`
        *   `Read Message History`
        *   `Add Reactions`
        *   `Connect` (Voice Permission)
        *   `Speak` (Voice Permission)
        *   *Optional but Recommended:* `Manage Messages` (allows deleting the Now Playing embed cleanly)
    *   Copy the generated URL at the bottom.
    *   Paste the URL into your web browser, select the server you want to add the bot to, and authorize it.

## Usage

*   **Default Prefix:** `,,`
*   **Help:** `,,help` or `/help`
*   **List Streams:** `,,list` or `/list`
*   **Play:** `,,play <URL or Predefined Name>` or `/play stream:<URL or Predefined Name>`
*   **Stop:** `,,stop` or `/stop` or react with ⏹️ on the Now Playing message.
*   **Show Now Playing:** `,,now` or `/now`
*   **Leave Channel:** `,,leave` or `,,dc`

## Managing the Bot Container

*   **View Logs:** See what the bot is doing (or view errors).
    ```bash
    docker-compose logs -f
    ```
    (Press `Ctrl+C` to stop following logs).
*   **Stop the Bot:** Gracefully stops and removes the container.
    ```bash
    docker-compose down
    ```
*   **Restart the Bot:**
    ```bash
    docker-compose restart
    ```
    (Or `docker-compose down` followed by `docker-compose up -d`).
*   **Update the Bot:** If you modify `bot.py` or `requirements.txt`:
    ```bash
    docker-compose build # Rebuild the image with changes
    docker-compose up -d # Restart the container with the new image
    ```

## Troubleshooting

*   **Bot Not Coming Online:**
    *   Check logs (`docker-compose logs -f`). Look for "Login Failed" (bad token in `.env`) or "Privileged Intents Required" (enable them in the developer portal).
    *   Ensure Docker is running.
    *   Verify the `DISCORD_TOKEN` in your `.env` file is correct and has no extra spaces/characters.
*   **Slash Commands Not Appearing:** Slash commands can sometimes take up to an hour to register globally after the bot first starts or syncs. Be patient. If they still don't appear, check logs for sync errors.
*   **Bot Doesn't Play Audio:** Ensure FFmpeg was installed correctly during the Docker build (check build logs or run `docker exec discord-radio-bot ffmpeg -version` while the container is running). Check radio stream URLs are valid.
*   **Permission Errors in Discord:** Make sure the bot has the necessary permissions in the specific channel/server (Send Messages, Embed Links, Connect, Speak, etc.).