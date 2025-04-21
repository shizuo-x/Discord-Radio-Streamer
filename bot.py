import discord
from discord.ext import commands, tasks
import os
import asyncio
import functools
import logging
import json # For state persistence
import datetime
import re # For parsing metadata
import aiohttp # For fetching metadata
from dotenv import load_dotenv

# --- Basic Logging Setup ---
# Consider using RotatingFileHandler for long-running bots
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord_bot')

# --- Configuration ---
load_dotenv()
BOT_TOKEN = os.getenv('DISCORD_TOKEN')
COMMAND_PREFIX = ",,"
RECONNECT_DELAY = 5
MAX_RECONNECT_ATTEMPTS = 3
STOP_REACTION = '⏹️'
STATE_FILE = 'state.json' # File for persistence
METADATA_FETCH_INTERVAL = 30 # Seconds between metadata checks

# --- Predefined Radio Streams ---
PREDEFINED_STREAMS = {
    "example": "https://example.com/example.mp3",
}

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.reactions = True

# --- Bot Initialization ---
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)
# Use aiohttp ClientSession for efficient HTTP requests
bot.http_session = None # Will be initialized in on_ready

# --- Guild Playback State ---
# In-memory cache, loaded from/saved to STATE_FILE
# {guild_id: {"vc": vc|None, "url": str, "stream_name": str, "should_play": bool, "retries": int,
#             "requester_id": int|None, "text_channel_id": int|None, "voice_channel_id": int|None,
#             "now_playing_message_id": int|None, "current_metadata": str|None, "is_resuming": bool}}
guild_states = {}

# --- Persistence Functions ---

def save_state():
    """Saves the relevant parts of guild_states to state.json for persistence."""
    persistent_state = {}
    for guild_id, state in guild_states.items():
        # Only save if the bot is supposed to be playing
        if state.get('should_play') and state.get('voice_channel_id') and state.get('url'):
            persistent_state[str(guild_id)] = {
                'voice_channel_id': state['voice_channel_id'],
                'text_channel_id': state.get('text_channel_id'), # Store text channel too
                'stream_url': state['url'],
                'stream_name': state.get('stream_name', state['url']), # Fallback to URL if name missing
                'requester_id': state.get('requester_id'), # Store requester ID
            }
            logger.debug(f"[{guild_id}] Preparing to save state: VC={state['voice_channel_id']}, URL={state['url']}")
        else:
            logger.debug(f"[{guild_id}] Skipping save for guild state (should_play=False or missing info).")

    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(persistent_state, f, indent=4)
        logger.info(f"Successfully saved state for {len(persistent_state)} guild(s) to {STATE_FILE}")
    except IOError as e:
        logger.error(f"Error saving state to {STATE_FILE}: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error saving state: {e}")

def load_state():
    """Loads persistent state from state.json into guild_states."""
    global guild_states
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                loaded_data = json.load(f)
                temp_states = {}
                # Convert keys back to int, initialize runtime fields
                for guild_id_str, saved_state in loaded_data.items():
                    try:
                        guild_id = int(guild_id_str)
                        temp_states[guild_id] = {
                            'vc': None, # VoiceClient needs to be re-established
                            'url': saved_state.get('stream_url'),
                            'stream_name': saved_state.get('stream_name'),
                            'should_play': True, # Assume it should play if saved
                            'retries': 0,
                            'requester_id': saved_state.get('requester_id'),
                            'text_channel_id': saved_state.get('text_channel_id'),
                            'voice_channel_id': saved_state.get('voice_channel_id'),
                            'now_playing_message_id': None, # Message needs to be resent
                            'current_metadata': None,
                            'is_resuming': True # Flag to indicate this state came from persistence
                        }
                        logger.info(f"[{guild_id}] Loaded saved state: VC={saved_state.get('voice_channel_id')}, URL={saved_state.get('stream_url')}")
                    except (ValueError, KeyError, TypeError) as e:
                        logger.error(f"Error processing saved state for guild '{guild_id_str}': {e} - Skipping.")
                guild_states = temp_states
                logger.info(f"Successfully loaded state for {len(guild_states)} guild(s) from {STATE_FILE}")
        else:
            logger.info(f"{STATE_FILE} not found, starting with empty state.")
            guild_states = {}
    except (IOError, json.JSONDecodeError) as e:
        logger.error(f"Error loading state from {STATE_FILE}: {e}. Starting with empty state.")
        guild_states = {}
    except Exception as e:
        logger.exception(f"Unexpected error loading state: {e}. Starting with empty state.")
        guild_states = {}

# --- Helper Functions ---

async def cleanup_now_playing_message(guild_id: int):
    """Safely deletes the existing 'Now Playing' message."""
    state = guild_states.get(guild_id)
    if not state: return # No state for guild

    message_id = state.get('now_playing_message_id')
    channel_id = state.get('text_channel_id')
    state['now_playing_message_id'] = None # Clear ID immediately

    if message_id and channel_id:
        try:
            guild = bot.get_guild(guild_id)
            if not guild: return
            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel): return

            message = await channel.fetch_message(message_id)
            await message.delete()
            logger.info(f"[{guild_id}] Deleted previous 'Now Playing' message (ID: {message_id}).")
        except discord.NotFound:
            logger.debug(f"[{guild_id}] Previous 'Now Playing' message {message_id} not found (already deleted?).")
        except discord.Forbidden:
            logger.warning(f"[{guild_id}] Missing permissions to delete 'Now Playing' message {message_id}.")
        except Exception as e:
            logger.error(f"[{guild_id}] Error deleting 'Now Playing' message {message_id}: {e}", exc_info=True)

async def send_or_edit_now_playing_embed(guild_id: int, force_new: bool = False):
    """Creates/sends or edits the 'Now Playing' embed."""
    state = guild_states.get(guild_id)
    if not state or not state.get('should_play'):
        logger.debug(f"[{guild_id}] send_or_edit_now_playing called but should_play is false.")
        await cleanup_now_playing_message(guild_id) # Ensure cleanup if state changed rapidly
        return

    guild = bot.get_guild(guild_id)
    channel_id = state.get('text_channel_id')
    if not guild or not channel_id:
        logger.error(f"[{guild_id}] Cannot send/edit embed: Guild or text_channel_id missing."); return

    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        logger.warning(f"[{guild_id}] Cannot send/edit embed: Channel {channel_id} not found or not text."); return

    # --- Create Embed Content ---
    embed = discord.Embed(
        title="▶️ Now Playing",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    stream_name = state.get('stream_name', 'Unknown Stream')
    requester_id = state.get('requester_id')
    requester = await bot.fetch_user(requester_id) if requester_id else None # Fetch user object
    requester_mention = requester.mention if requester else "Unknown"
    metadata = state.get('current_metadata')

    embed.add_field(name="Stream", value=f"`{stream_name}`", inline=False)
    if metadata:
        embed.add_field(name="Current Track", value=f"```{metadata}```", inline=False) # Use code block for better formatting
    embed.add_field(name="Requested By", value=requester_mention, inline=False)
    embed.add_field(name="Playback Position", value="🔵 **LIVE**", inline=False)
    try: embed.set_footer(text=f"{bot.user.name} Radio", icon_url=bot.user.display_avatar.url)
    except: embed.set_footer(text=f"{bot.user.name} Radio")

    # --- Send or Edit Logic ---
    message_id = state.get('now_playing_message_id')
    message = None

    # Try editing if not forced new and message ID exists
    if not force_new and message_id:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            logger.debug(f"[{guild_id}] Edited 'Now Playing' embed (ID: {message_id}).")
            return # Success editing
        except discord.NotFound:
            logger.info(f"[{guild_id}] Now Playing message {message_id} not found for editing, sending new one.")
            message_id = None # Force sending new below
            state['now_playing_message_id'] = None
        except discord.Forbidden:
            logger.warning(f"[{guild_id}] Missing permissions to edit Now Playing message {message_id}.")
            # Can't edit, try sending new if needed
        except Exception as e:
            logger.error(f"[{guild_id}] Error editing Now Playing message {message_id}: {e}")
            # Can't edit, try sending new

    # Send new message if forced, previous edit failed, or no previous message ID
    if not message_id or force_new:
        await cleanup_now_playing_message(guild_id) # Clean up any potential lingering old message
        try:
            new_message = await channel.send(embed=embed)
            state['now_playing_message_id'] = new_message.id
            logger.info(f"[{guild_id}] Sent new 'Now Playing' embed (ID: {new_message.id})")
            try:
                await new_message.add_reaction(STOP_REACTION)
            except Exception as react_error:
                logger.warning(f"[{guild_id}] Failed to add reaction to new message {new_message.id}: {react_error}")
        except discord.Forbidden:
            logger.warning(f"[{guild_id}] Missing permissions to send embed or add reactions in channel {channel.id}.")
            state['now_playing_message_id'] = None # Ensure cleared on failure
        except Exception as e:
            logger.error(f"[{guild_id}] Error sending new 'Now Playing' embed: {e}", exc_info=True)
            state['now_playing_message_id'] = None

async def _play_internal(guild_id: int, voice_client: discord.VoiceClient):
    """Internal logic to start FFmpeg playback."""
    state = guild_states.get(guild_id)
    if not state or not state['should_play']:
        logger.warning(f"[{guild_id}] _play_internal called but should_play is false or state missing.")
        return

    stream_url = state.get('url')
    stream_name = state.get('stream_name', 'Unknown Stream')
    if not stream_url:
        logger.error(f"[{guild_id}] _play_internal called but stream_url is missing.")
        state['should_play'] = False
        save_state()
        return

    try:
        if voice_client.is_playing() or voice_client.is_paused():
            logger.info(f"[{guild_id}] Stopping existing playback before starting new stream '{stream_name}'.")
            voice_client.stop()
            await asyncio.sleep(0.5) # Short delay

        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 5000000 -probesize 5000000', # Added probe/analyze duration
            'options': '-vn -loglevel warning' # Suppress verbose ffmpeg logs, show warnings/errors
        }
        audio_source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)

        after_callback = functools.partial(after_playback_handler, guild_id)
        voice_client.play(audio_source, after=after_callback)

        logger.info(f"[{guild_id}] Playback started via FFmpeg for stream: {stream_name} ({stream_url})")
        state['retries'] = 0 # Reset retries on successful play start
        state['is_resuming'] = False # No longer resuming once playback starts
        save_state() # Save state after successful start

        # Send embed after starting
        await send_or_edit_now_playing_embed(guild_id, force_new=True) # Force new on initial play/resume

    except discord.errors.ClientException as e:
        logger.error(f"[{guild_id}] discord.py ClientException during play setup for '{stream_name}': {e}")
        state['should_play'] = False
        save_state()
        # Optionally notify text channel
    except Exception as e:
        logger.error(f"[{guild_id}] Error starting FFmpeg playback for '{stream_name}': {e}", exc_info=True)
        state['should_play'] = False
        save_state()
        # Optionally notify text channel

async def ensure_voice_and_play(guild_id: int, voice_channel_id: int, text_channel_id: int | None, stream_url: str, stream_name: str, requester_id: int | None, is_manual_play: bool = False):
    """Connects/moves to VC and initiates playback. Handles state updates."""
    guild = bot.get_guild(guild_id)
    if not guild:
        logger.error(f"[{guild_id}] ensure_voice_and_play: Guild not found.")
        return "Error: Guild not found."

    voice_channel = guild.get_channel(voice_channel_id)
    if not voice_channel or not isinstance(voice_channel, discord.VoiceChannel):
        logger.error(f"[{guild_id}] ensure_voice_and_play: Voice channel {voice_channel_id} not found or invalid.")
        return f"Error: Voice channel not found."

    # Update state *before* attempting connection/play
    if guild_id not in guild_states: guild_states[guild_id] = {}
    guild_states[guild_id].update({
        'url': stream_url,
        'stream_name': stream_name,
        'requester_id': requester_id,
        'text_channel_id': text_channel_id,
        'voice_channel_id': voice_channel_id,
        'should_play': True,
        'retries': guild_states[guild_id].get('retries', 0), # Keep existing retries during reconnect attempts
        'vc': guild.voice_client, # Get current VC, might be None
        'now_playing_message_id': guild_states[guild_id].get('now_playing_message_id'), # Keep existing message ID if reconnecting
        'current_metadata': None, # Reset metadata on new play/reconnect
        'is_resuming': not is_manual_play, # Mark as resuming if not triggered by a user play command
    })
    logger.info(f"[{guild_id}] Updating state: should_play=True, VC ID={voice_channel_id}, URL={stream_url}")

    voice_client = guild.voice_client # Get current VC

    try:
        if voice_client and voice_client.is_connected():
            if voice_client.channel != voice_channel:
                logger.info(f"[{guild_id}] Moving to voice channel: {voice_channel.name}")
                await voice_client.move_to(voice_channel)
            else:
                 logger.info(f"[{guild_id}] Already connected to the correct voice channel: {voice_channel.name}")
        else:
            logger.info(f"[{guild_id}] Connecting to voice channel: {voice_channel.name}")
            voice_client = await voice_channel.connect(timeout=60.0, reconnect=True)
            guild_states[guild_id]['vc'] = voice_client # Store the new VC object

        # Ensure VC object is stored correctly
        if not voice_client or not voice_client.is_connected():
            raise Exception("Failed to connect or store voice client.")

        # --- Initiate Playback ---
        await _play_internal(guild_id, voice_client)
        return f"▶️ Now playing: `{stream_name}`"

    except asyncio.TimeoutError:
         logger.error(f"[{guild_id}] Timeout connecting/moving to voice channel: {voice_channel.name}")
         guild_states[guild_id]['should_play'] = False
         save_state()
         return "Error: Timed out connecting to the voice channel."
    except discord.errors.ClientException as e:
         logger.error(f"[{guild_id}] ClientException connecting/moving: {e}")
         # This might mean already connecting, check current state
         if guild.voice_client and guild.voice_client.is_connected():
              logger.warning(f"[{guild_id}] ClientException but already connected, attempting play anyway.")
              guild_states[guild_id]['vc'] = guild.voice_client
              await _play_internal(guild_id, guild.voice_client)
              return f"▶️ Now playing: `{stream_name}`"
         else:
              guild_states[guild_id]['should_play'] = False
              save_state()
              return f"Error connecting: {e}. Try `{COMMAND_PREFIX}leave` first."
    except Exception as e:
        logger.error(f"[{guild_id}] Error in ensure_voice_and_play for '{stream_name}': {e}", exc_info=True)
        guild_states[guild_id]['should_play'] = False
        save_state()
        return f"An error occurred: {e}"

def after_playback_handler(guild_id: int, error: Exception | None):
    """Callback after playback ends or errors. Handles state and reconnection."""
    state = guild_states.get(guild_id)
    if not state:
        logger.warning(f"[{guild_id}] after_playback_handler called but no state found.")
        return

    should_play = state.get('should_play', False) # Check intent *before* modifying state
    logger.info(f"[{guild_id}] Playback finished/stopped. Error: {error}, should_play flag was: {should_play}")

    # Cleanup embed regardless of error or should_play state
    # Use create_task as this handler runs in a separate thread
    asyncio.create_task(cleanup_now_playing_message(guild_id))

    if error:
        logger.error(f"[{guild_id}] Playback Error reported: {error}")
        if should_play:
            state['retries'] = state.get('retries', 0) + 1
            if state['retries'] <= MAX_RECONNECT_ATTEMPTS:
                logger.warning(f"[{guild_id}] Playback error while should_play=True. Attempting reconnect {state['retries']}/{MAX_RECONNECT_ATTEMPTS} in {RECONNECT_DELAY}s.")
                # Schedule reconnect task
                asyncio.create_task(reconnect_after_delay(guild_id))
            else:
                logger.error(f"[{guild_id}] Max reconnect attempts ({MAX_RECONNECT_ATTEMPTS}) reached after error. Stopping playback permanently.")
                state['should_play'] = False
                save_state() # Save the stopped state
        else:
            logger.info(f"[{guild_id}] Playback error occurred, but should_play=False (manual stop during error?). Not attempting reconnect.")
            # Ensure state reflects stopped status
            state['should_play'] = False
            state['retries'] = 0
            save_state()
    else:
        # Playback finished without error (manual stop, or potentially stream ending cleanly - less common for radio)
        logger.info(f"[{guild_id}] Playback ended without error. Assuming manual stop or natural end.")
        # Ensure state reflects stopped status
        state['should_play'] = False
        state['retries'] = 0
        save_state()

async def reconnect_after_delay(guild_id: int):
    """Waits and then attempts to reconnect and play."""
    await asyncio.sleep(RECONNECT_DELAY)
    state = guild_states.get(guild_id)
    # Re-check state after delay
    if not state or not state.get('should_play'):
        logger.info(f"[{guild_id}] Reconnect cancelled after delay (state changed or removed).")
        return

    logger.info(f"[{guild_id}] Executing reconnect attempt {state.get('retries', '?')}")
    voice_channel_id = state.get('voice_channel_id')
    text_channel_id = state.get('text_channel_id')
    stream_url = state.get('url')
    stream_name = state.get('stream_name')
    requester_id = state.get('requester_id')

    if not all([voice_channel_id, stream_url, stream_name]):
        logger.error(f"[{guild_id}] Cannot reconnect: Missing required state info (VC ID, URL, or Name). Stopping.")
        state['should_play'] = False
        save_state()
        return

    # Call the main function to handle connection and playing
    await ensure_voice_and_play(guild_id, voice_channel_id, text_channel_id, stream_url, stream_name, requester_id, is_manual_play=False)


# --- Metadata Fetching Task ---
@tasks.loop(seconds=METADATA_FETCH_INTERVAL)
async def fetch_metadata_loop():
    # Ensure session exists
    if not bot.http_session or bot.http_session.closed:
        logger.warning("Metadata loop: aiohttp session closed or not initialized, skipping cycle.")
        # Attempt to recreate session if closed
        if bot.http_session and bot.http_session.closed:
             bot.http_session = aiohttp.ClientSession()
        return

    # Iterate over a copy of keys in case state changes during iteration
    active_guild_ids = list(guild_states.keys())

    for guild_id in active_guild_ids:
        state = guild_states.get(guild_id)
        # Check if bot should be playing and has necessary info
        if state and state.get('should_play') and state.get('url') and state.get('vc') and state['vc'].is_playing():
            stream_url = state['url']
            logger.debug(f"[{guild_id}] Attempting metadata fetch for: {stream_url}")
            metadata = None
            try:
                headers = {'Icy-Metadata': '1'}
                # Short timeout to avoid blocking the loop for too long
                async with bot.http_session.get(stream_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if 200 <= response.status < 300:
                        metaint_header = response.headers.get('icy-metaint')
                        if metaint_header:
                            try:
                                metaint = int(metaint_header)
                                # Read up to the metadata block + some buffer
                                # Reading large amounts can be slow/memory intensive, be careful
                                chunk = await response.content.readexactly(metaint + 256 * 16) # Read metadata interval + buffer for metadata length/content
                                metadata_length = chunk[metaint] * 16 # Length byte after the interval
                                if metadata_length > 0:
                                     metadata_bytes = chunk[metaint + 1 : metaint + 1 + metadata_length]
                                     metadata_text = metadata_bytes.decode('utf-8', errors='ignore').strip()
                                     # Extract title using regex
                                     match = re.search(r"StreamTitle='([^;]*)';", metadata_text)
                                     if match:
                                         metadata = match.group(1).strip()
                                         logger.debug(f"[{guild_id}] Parsed metadata: {metadata}")
                                     else:
                                         logger.debug(f"[{guild_id}] Could not parse StreamTitle from metadata block: {metadata_text}")
                                else:
                                     logger.debug(f"[{guild_id}] Metadata block length is zero.")

                            except (ValueError, IndexError, asyncio.exceptions.IncompleteReadError) as e:
                                logger.debug(f"[{guild_id}] Error parsing metadata structure for {stream_url}: {e}")
                            except Exception as e:
                                logger.warning(f"[{guild_id}] Unexpected error processing metadata chunk for {stream_url}: {e}")
                        else:
                            logger.debug(f"[{guild_id}] Stream {stream_url} does not provide icy-metaint header.")
                    else:
                        logger.debug(f"[{guild_id}] Metadata fetch failed for {stream_url}, status: {response.status}")

                # Update state and embed if metadata changed
                if metadata and metadata != state.get('current_metadata'):
                    logger.info(f"[{guild_id}] Updating metadata: '{metadata}'")
                    state['current_metadata'] = metadata
                    await send_or_edit_now_playing_embed(guild_id) # Edit the existing embed
                elif not metadata and state.get('current_metadata') is not None:
                     # Metadata disappeared, clear it
                     logger.info(f"[{guild_id}] Clearing previous metadata.")
                     state['current_metadata'] = None
                     await send_or_edit_now_playing_embed(guild_id)


            except asyncio.TimeoutError:
                logger.debug(f"[{guild_id}] Timeout fetching metadata for {stream_url}.")
            except aiohttp.ClientError as e:
                logger.warning(f"[{guild_id}] Network error fetching metadata for {stream_url}: {e}")
            except Exception as e:
                logger.exception(f"[{guild_id}] Unexpected error in metadata fetch loop for {stream_url}: {e}")
        else:
             logger.debug(f"[{guild_id}] Skipping metadata fetch (not playing or missing info).")


@fetch_metadata_loop.before_loop
async def before_metadata_loop():
    await bot.wait_until_ready() # Wait for the bot to be ready

# --- Bot Events ---
@bot.event
async def on_ready():
    """Called when the bot is ready and after reconnections."""
    if bot.http_session is None or bot.http_session.closed:
         bot.http_session = aiohttp.ClientSession()
         logger.info("Created new aiohttp ClientSession.")

    logger.info(f"Logged in as {bot.user.name} ({bot.user.id})")
    logger.info(f"Command Prefix: {COMMAND_PREFIX}")
    logger.info(f"discord.py version: {discord.__version__}")
    logger.info("------")

    if not hasattr(bot, 'synced_commands'): # Sync commands only once on first ready
        try:
            synced = await bot.tree.sync()
            logger.info(f"Synced {len(synced)} application (slash) command(s).")
            bot.synced_commands = True
        except Exception as e:
            logger.exception(f"Failed to sync slash commands: {e}")
            bot.synced_commands = False # Allow retry on next ready if failed

    if not hasattr(bot, 'loaded_state'): # Load state only once
        load_state()
        bot.loaded_state = True
        logger.info("Attempting auto-resume for saved states...")
        # --- Auto-Resume Logic ---
        for guild_id, state in list(guild_states.items()): # Iterate over copy
            if state.get('is_resuming'): # Check the flag set during load_state
                logger.info(f"[{guild_id}] Found resumable state. Attempting auto-play.")
                vc_id = state.get('voice_channel_id')
                txt_id = state.get('text_channel_id')
                url = state.get('url')
                name = state.get('stream_name')
                req_id = state.get('requester_id')
                if all([vc_id, url, name]):
                    # Use create_task to avoid blocking on_ready
                    asyncio.create_task(
                        ensure_voice_and_play(guild_id, vc_id, txt_id, url, name, req_id, is_manual_play=False)
                    )
                else:
                    logger.warning(f"[{guild_id}] Cannot auto-resume: Missing required state info.")
                    state['should_play'] = False # Mark as not playing if info missing
                    state['is_resuming'] = False

    # Start background tasks if not already running
    if not fetch_metadata_loop.is_running():
        logger.info("Starting metadata fetch loop.")
        fetch_metadata_loop.start()

    # --- Post-Reconnect Check ---
    # Check guilds where bot *thought* it was playing before disconnect
    for guild_id, state in list(guild_states.items()):
        guild = bot.get_guild(guild_id)
        if guild and state.get('should_play') and not state.get('is_resuming'): # Only check if not actively resuming
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                 logger.warning(f"[{guild_id}] Bot reconnected, but voice client is missing/disconnected while should_play=True. Attempting reconnect.")
                 # Reset retries for reconnect after gateway issues
                 state['retries'] = 0
                 # Trigger reconnect logic
                 asyncio.create_task(reconnect_after_delay(guild_id))


@bot.event
async def on_disconnect():
    """Called when the main gateway connection is lost."""
    logger.warning("Bot disconnected from Discord Gateway!")
    # State saving on disconnect can be risky if it happens abruptly
    # Rely on periodic saves or saving on graceful shutdown instead

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes."""
    if member.id == bot.user.id: # Bot's voice state changed
        guild_id = member.guild.id
        state = guild_states.get(guild_id)

        if before.channel and not after.channel: # Bot disconnected from a channel
            logger.info(f"[{guild_id}] Bot disconnected from voice channel '{before.channel.name}'. Source: {'API' if after.channel is None else 'Moved'}")
            if state:
                state['vc'] = None # Clear VC object
                # Check if disconnect was expected (due to should_play=False)
                if state.get('should_play'):
                    logger.warning(f"[{guild_id}] Bot disconnected unexpectedly while should_play=True! Attempting reconnect.")
                    state['retries'] = 0 # Reset retries for unexpected disconnect
                    asyncio.create_task(reconnect_after_delay(guild_id))
                else:
                    logger.info(f"[{guild_id}] Bot disconnect was expected (should_play=False). Resetting state.")
                    state['retries'] = 0
                    save_state() # Save the stopped state
                    await cleanup_now_playing_message(guild_id)

        elif not before.channel and after.channel: # Bot connected to a channel
            logger.info(f"[{guild_id}] Bot connected to voice channel '{after.channel.name}'.")
            # Update state if needed (usually handled by ensure_voice_and_play)
            if state: state['vc'] = member.guild.voice_client

        elif before.channel != after.channel: # Bot moved channels
             logger.info(f"[{guild_id}] Bot moved from '{before.channel.name}' to '{after.channel.name}'.")
             if state: state['vc'] = member.guild.voice_client # Update VC


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User | discord.Member):
    """Handle stop reaction."""
    if user.bot or not reaction.message.guild: return

    guild_id = reaction.message.guild.id
    state = guild_states.get(guild_id)

    if state and str(reaction.emoji) == STOP_REACTION and reaction.message.id == state.get('now_playing_message_id'):
        logger.info(f"[{guild_id}] Stop reaction detected from user {user.name} on message {reaction.message.id}")
        vc = reaction.message.guild.voice_client
        if vc and vc.is_connected():
            state['should_play'] = False # Set intent *before* stopping
            logger.info(f"[{guild_id}] Stopping playback via reaction.")
            if vc.is_playing() or vc.is_paused():
                vc.stop() # Triggers after_playback_handler -> save_state & cleanup
            else: # If connected but not playing, still need to save state and clean embed
                 save_state()
                 await cleanup_now_playing_message(guild_id)
            try: await reaction.remove(user)
            except: pass # Ignore permission errors removing reaction
            try: await reaction.message.channel.send(f"⏹️ Playback stopped by {user.mention}.", delete_after=10)
            except: pass
        else:
            logger.info(f"[{guild_id}] Stop reaction detected, but bot not connected.")
            # If message exists but bot isn't connected, ensure state is clean
            state['should_play'] = False
            save_state()
            await cleanup_now_playing_message(guild_id)


# --- Commands (Prefix & Slash) ---

# Help Command
async def send_help_embed(ctx_or_interaction):
    is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
    ephemeral = is_interaction
    embed = discord.Embed( title=f"{bot.user.name} Help", description=f"Radio bot. Prefix: `{COMMAND_PREFIX}`. Also uses Slash Commands.", color=discord.Color.blue())
    try: embed.set_thumbnail(url=bot.user.display_avatar.url)
    except: pass
    embed.add_field( name="🔊 Voice Commands", value=f"`{COMMAND_PREFIX}play <URL or Name>` or `/play stream:<URL or Name>`\nPlays a live radio stream from URL or `{COMMAND_PREFIX}list`.\n\n`{COMMAND_PREFIX}stop` or `/stop`\nStops playback.\n\n`{COMMAND_PREFIX}leave` or `{COMMAND_PREFIX}dc`\nDisconnects the bot.\n\n`{COMMAND_PREFIX}now` or `/now`\nShows the current stream info.", inline=False)
    embed.add_field( name="ℹ️ Utility Commands", value=f"`{COMMAND_PREFIX}help` or `/help`\nShows this message.\n\n`{COMMAND_PREFIX}list` or `/list`\nShows predefined streams.\n\n`{COMMAND_PREFIX}ping`\nChecks latency.", inline=False)
    embed.add_field( name="▶️ Playback Control", value=f"React with {STOP_REACTION} on the 'Now Playing' message to stop.", inline=False)
    embed.set_footer(text="Enjoy!")
    if isinstance(ctx_or_interaction, commands.Context): await ctx_or_interaction.send(embed=embed)
    elif is_interaction:
        try: await ctx_or_interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        except discord.errors.InteractionResponded: await ctx_or_interaction.followup.send(embed=embed, ephemeral=ephemeral)
@bot.command(name='help')
async def help_prefix(ctx): await send_help_embed(ctx)
@bot.tree.command(name="help", description="Shows the bot's help information.")
async def help_slash(interaction: discord.Interaction): await send_help_embed(interaction)

# List Command
async def send_list_embed(ctx_or_interaction):
    is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
    ephemeral = is_interaction
    if not PREDEFINED_STREAMS: desc = "No predefined streams configured."
    else: desc = f"Use these names with `{COMMAND_PREFIX}play <Name>` or `/play stream:<Name>`:\n" + "\n".join(f"- `{name}`" for name in PREDEFINED_STREAMS.keys())
    embed = discord.Embed( title="📻 Predefined Radio Streams", description=desc, color=discord.Color.orange())
    if isinstance(ctx_or_interaction, commands.Context): await ctx_or_interaction.send(embed=embed)
    elif is_interaction:
        try: await ctx_or_interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        except discord.errors.InteractionResponded: await ctx_or_interaction.followup.send(embed=embed, ephemeral=ephemeral)
@bot.command(name='list')
async def list_prefix(ctx): await send_list_embed(ctx)
@bot.tree.command(name="list", description="Shows the list of predefined radio streams.")
async def list_slash(interaction: discord.Interaction): await send_list_embed(interaction)

# Ping Command
@bot.command(name='ping')
async def ping_prefix(ctx): await ctx.send(f"Pong! Latency: {bot.latency * 1000:.2f} ms")

# Play Command
async def _play_command_logic(guild_id: int, user: discord.User | discord.Member, text_channel_id: int | None, voice_channel: discord.VoiceChannel | None, stream_input: str):
    """Shared logic for prefix and slash play commands."""
    if not voice_channel:
        return "You need to be in a voice channel to use this command."
    if not text_channel_id:
         logger.error(f"[{guild_id}] Play command failed: Could not determine text channel.")
         return "Error: Could not determine the text channel for communication."

    stream_url = stream_input.strip('<>')
    stream_name = stream_url

    matched_name = next((name for name in PREDEFINED_STREAMS if name.lower() == stream_url.lower()), None)
    if matched_name:
        stream_url = PREDEFINED_STREAMS[matched_name]
        stream_name = matched_name
        logger.info(f"[{guild_id}] Matched predefined stream: {stream_name}")
    elif not stream_url.startswith(('http://', 'https')):
         return f"Input `{stream_url}` is not a valid URL or predefined stream name. See `{COMMAND_PREFIX}list`."

    # Call the core function
    result = await ensure_voice_and_play(guild_id, voice_channel.id, text_channel_id, stream_url, stream_name, user.id, is_manual_play=True)
    return result

@bot.command(name='play', aliases=['p', 'stream'])
async def play_prefix(ctx, *, stream_url_or_name: str):
    result = await _play_command_logic(ctx.guild.id, ctx.author, ctx.channel.id, ctx.author.voice.channel if ctx.author.voice else None, stream_url_or_name)
    await ctx.send(result)

@bot.tree.command(name="play", description="Plays a radio stream URL or predefined name.")
@discord.app_commands.describe(stream="The URL or predefined name of the stream (see /list)")
async def play_slash(interaction: discord.Interaction, stream: str):
    try: await interaction.response.defer()
    except Exception as e: logger.error(f"[{interaction.guild_id}] Defer failed: {e}"); return
    result = await _play_command_logic(interaction.guild_id, interaction.user, interaction.channel_id, interaction.user.voice.channel if interaction.user.voice else None, stream)
    await interaction.followup.send(result)

# Stop Command
async def _stop_command_logic(guild_id: int):
    state = guild_states.get(guild_id)
    guild = bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None

    if state:
        state['should_play'] = False # Signal intent
        logger.info(f"[{guild_id}] Stop command used, setting should_play=False.")

    if vc and vc.is_connected():
        if vc.is_playing() or vc.is_paused():
            logger.info(f"[{guild_id}] Stopping playback via command.")
            vc.stop() # Triggers after_playback_handler -> save_state & cleanup
            return "⏹️ Playback stopped."
        else:
             if state: save_state(); await cleanup_now_playing_message(guild_id) # Explicit cleanup if stopped but connected
             return "Nothing was playing, but I am connected."
    else:
        if state: save_state(); await cleanup_now_playing_message(guild_id) # Ensure cleanup if message exists but not connected
        return "Not currently connected to a voice channel."

@bot.command(name='stop')
async def stop_prefix(ctx):
    result = await _stop_command_logic(ctx.guild.id)
    await ctx.send(result)

@bot.tree.command(name="stop", description="Stops the current audio stream.")
async def stop_slash(interaction: discord.Interaction):
    try: await interaction.response.defer()
    except Exception as e: logger.error(f"[{interaction.guild_id}] Defer failed: {e}"); return
    result = await _stop_command_logic(interaction.guild_id)
    await interaction.followup.send(result)

# Leave Command
@bot.command(name='leave', aliases=['dc'])
async def leave_prefix(ctx):
    guild_id = ctx.guild.id
    state = guild_states.get(guild_id)
    vc = ctx.guild.voice_client

    if state:
        state['should_play'] = False # Signal intent
        logger.info(f"[{guild_id}] Leave command used, setting should_play=False.")
        save_state() # Save stopped state before disconnect
        await cleanup_now_playing_message(guild_id) # Explicit cleanup

    if vc and vc.is_connected():
        channel_name = vc.channel.name
        logger.info(f"[{guild_id}] Disconnecting from '{channel_name}' via command.")
        await vc.disconnect(force=False) # Triggers voice_state_update
        await ctx.send(f"Left `{channel_name}`.")
    else:
        await ctx.send("Not currently connected.")

# Now Command
@bot.command(name='now', aliases=['np'])
async def now_prefix(ctx):
    state = guild_states.get(ctx.guild.id)
    if state and state.get('should_play') and state.get('vc') and state['vc'].is_playing():
        logger.info(f"[{ctx.guild.id}] Resending Now Playing embed via command.")
        await send_or_edit_now_playing_embed(ctx.guild.id, force_new=True) # Force recreate embed
        try: await ctx.message.delete() # Clean up command message
        except: pass
    else:
        await ctx.send("Not currently playing anything.")

@bot.tree.command(name="now", description="Shows the currently playing stream.")
async def now_slash(interaction: discord.Interaction):
    try: await interaction.response.defer(ephemeral=True)
    except Exception as e: logger.error(f"[{interaction.guild_id}] Defer failed: {e}"); return
    state = guild_states.get(interaction.guild_id)
    if state and state.get('should_play') and state.get('vc') and state['vc'].is_playing():
        logger.info(f"[{interaction.guild_id}] Resending Now Playing embed via slash command.")
        await send_or_edit_now_playing_embed(interaction.guild_id, force_new=True)
        await interaction.followup.send("Showing current stream info.", ephemeral=True)
    else:
        await interaction.followup.send("Not currently playing anything.", ephemeral=True)

# --- Error Handlers ---
@bot.event
async def on_command_error(ctx, error):
    # ... (Keep existing prefix error handler or refine) ...
    if isinstance(error, commands.CommandNotFound): pass
    elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"Missing argument: `{error.param.name}`. See `{COMMAND_PREFIX}help`.", delete_after=15)
    elif isinstance(error, commands.CommandInvokeError):
        original = error.original
        logger.error(f"Error in prefix command '{ctx.command}': {original}", exc_info=original)
        await ctx.send(f"An error occurred: ```{original}```")
    elif isinstance(error, commands.CheckFailure): await ctx.send("You lack permissions.", delete_after=15)
    else: logger.error(f"Unhandled prefix command error for '{ctx.command}': {error}", exc_info=error)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    # ... (Keep existing slash error handler or refine) ...
    if isinstance(error, discord.app_commands.CommandInvokeError): error_message = f"Internal error: ```{error.original}```"; logger.error(f"Slash cmd '{interaction.command.name}': {error.original}", exc_info=error.original)
    elif isinstance(error, discord.app_commands.CheckFailure): error_message = "You lack permissions."; logger.warning(f"Slash cmd check failed '{interaction.command.name}' by {interaction.user}: {error}")
    else: error_message = "An unexpected error occurred."; logger.error(f"Unhandled slash cmd error '{interaction.command.name}': {error}", exc_info=error)
    try:
        if not interaction.response.is_done(): await interaction.response.send_message(error_message, ephemeral=True)
        else: await interaction.followup.send(error_message, ephemeral=True)
    except Exception as e: logger.error(f"Failed to send error message for slash command '{interaction.command.name}': {e}")


# --- Graceful Shutdown ---
async def close_sessions():
    if bot.http_session and not bot.http_session.closed:
        await bot.http_session.close()
        logger.info("Closed aiohttp session.")

@bot.event
async def on_close():
    logger.info("Bot is closing. Saving final state.")
    save_state() # Save state on close
    await close_sessions()

# --- Run the Bot ---
async def main():
    async with bot:
        bot.http_session = aiohttp.ClientSession() # Initialize session
        if not BOT_TOKEN:
            logger.critical("CRITICAL ERROR: DISCORD_TOKEN environment variable not set.")
            return
        try:
            await bot.start(BOT_TOKEN)
        except discord.errors.LoginFailure:
            logger.critical("CRITICAL ERROR: Login Failed - Improper token.")
        except discord.errors.PrivilegedIntentsRequired as e:
            logger.critical(f"CRITICAL ERROR: Privileged Intents ({e.shard_id}) required but not enabled!")
        except Exception as e:
             logger.critical(f"CRITICAL ERROR running bot: {e}", exc_info=True)
        finally:
             logger.info("Bot process ending. Performing final cleanup.")
             await close_sessions() # Ensure session closed even on error exit


if __name__ == "__main__":
    try:
        # Use asyncio.run() to handle the async main function and cleanup
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down.")
    # Normal exit after asyncio.run completes or KeyboardInterrupt
    logger.info("Shutdown complete.")