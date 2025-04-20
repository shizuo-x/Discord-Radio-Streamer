import discord
from discord.ext import commands
import os
import asyncio
import functools # For passing arguments to the 'after' callback
import logging
from dotenv import load_dotenv # Optional: if using .env file
import datetime # For embed timestamp

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord_bot')

# --- Configuration ---
load_dotenv()
BOT_TOKEN = os.getenv('DISCORD_TOKEN')
COMMAND_PREFIX = ",," # Changed default prefix
RECONNECT_DELAY = 5
MAX_RECONNECT_ATTEMPTS = 3
STOP_REACTION = '⏹️' # Emoji used for the stop reaction

# --- Predefined Radio Streams ---
# Add your desired streams here (Name: URL)
# Example list - replace with your actual streams
PREDEFINED_STREAMS = {
    "station1": "link_here", # Replace with actual working URLs
    "station2": "link_here", # Replace with actual working URLs
}

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True # Needed for prefix commands
intents.voice_states = True    # Needed for voice
intents.guilds = True          # Needed for guild info & slash commands
intents.reactions = True       # Needed for reaction handling

# --- Bot Initialization ---
# Allow changing prefix per guild later if needed, but use default for now
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None) # Disable default help

# --- Guild Playback State ---
# {guild_id: {"vc": vc, "url": str, "stream_name": str, "should_play": bool,
#             "retries": int, "requester": discord.User/Member, "text_channel_id": int,
#             "now_playing_message_id": int | None}}
guild_states = {}

# --- Helper Functions ---

async def cleanup_now_playing_message(guild_id: int):
    """Safely deletes the existing 'Now Playing' message for a guild."""
    if guild_id in guild_states and guild_states[guild_id].get('now_playing_message_id'):
        try:
            guild = bot.get_guild(guild_id)
            if not guild: return
            channel_id = guild_states[guild_id].get('text_channel_id')
            if not channel_id: return
            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel): return

            msg_id = guild_states[guild_id]['now_playing_message_id']
            message = await channel.fetch_message(msg_id)
            await message.delete()
            logger.info(f"[{guild_id}] Deleted previous 'Now Playing' message (ID: {msg_id}).")
        except discord.NotFound:
            logger.info(f"[{guild_id}] Previous 'Now Playing' message not found (already deleted?).")
        except discord.Forbidden:
            logger.warning(f"[{guild_id}] Missing permissions to delete 'Now Playing' message.")
        except Exception as e:
            logger.error(f"[{guild_id}] Error deleting 'Now Playing' message: {e}", exc_info=True)
        finally:
             # Ensure ID is cleared even if deletion failed, to prevent reacting to old messages
             if guild_id in guild_states:
                 guild_states[guild_id]['now_playing_message_id'] = None


async def send_now_playing_embed(guild_id: int):
    """Creates and sends the 'Now Playing' embed, cleaning up the old one."""
    if guild_id not in guild_states or not guild_states[guild_id].get('should_play'):
        logger.debug(f"[{guild_id}] send_now_playing_embed called but should_play is false.")
        return # Don't send if not supposed to be playing

    state = guild_states[guild_id]
    guild = bot.get_guild(guild_id)
    if not guild:
        logger.error(f"[{guild_id}] Cannot send Now Playing embed: Guild not found.")
        return

    channel_id = state.get('text_channel_id')
    if not channel_id:
        logger.warning(f"[{guild_id}] Cannot send Now Playing embed: text_channel_id not found in state.")
        return

    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        logger.warning(f"[{guild_id}] Cannot send Now Playing embed: Channel {channel_id} not found or not text.")
        return

    # --- Cleanup previous message ---
    # Run this first to avoid having two embeds visible momentarily
    await cleanup_now_playing_message(guild_id)

    # --- Create Embed ---
    embed = discord.Embed(
        title="▶️ Now Playing",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    stream_name = state.get('stream_name', 'Unknown Stream')
    requester = state.get('requester', None)
    requester_mention = requester.mention if requester else "Unknown"

    embed.add_field(name="Stream", value=f"`{stream_name}`", inline=False)
    embed.add_field(name="Requested By", value=requester_mention, inline=False)
    embed.add_field(name="Playback Position", value="🔵 **LIVE**", inline=False)

    try: # Use bot's avatar if possible
        bot_avatar_url = bot.user.display_avatar.url
        embed.set_footer(text=f"{bot.user.name} Radio", icon_url=bot_avatar_url)
    except Exception: # Fallback if avatar unavailable
        embed.set_footer(text=f"{bot.user.name} Radio")


    try:
        message = await channel.send(embed=embed)
        # Important: Update state *after* sending successfully
        guild_states[guild_id]['now_playing_message_id'] = message.id
        logger.info(f"[{guild_id}] Sent 'Now Playing' embed (ID: {message.id})")

        # Add stop reaction *after* storing message ID
        try:
            await message.add_reaction(STOP_REACTION)
        except Exception as react_error:
            logger.warning(f"[{guild_id}] Failed to add reaction to message {message.id}: {react_error}")

    except discord.Forbidden:
        logger.warning(f"[{guild_id}] Missing permissions to send embed or add reactions in channel {channel.id}.")
        # Ensure ID is cleared if send fails
        if guild_id in guild_states:
             guild_states[guild_id]['now_playing_message_id'] = None
    except Exception as e:
        logger.error(f"[{guild_id}] Error sending 'Now Playing' embed: {e}", exc_info=True)
        # Ensure ID is cleared if send fails
        if guild_id in guild_states:
            guild_states[guild_id]['now_playing_message_id'] = None


async def ensure_voice(ctx_or_interaction):
    """Checks user voice state, connects/moves bot, returns voice_client."""
    guild = ctx_or_interaction.guild
    if isinstance(ctx_or_interaction, commands.Context):
        user = ctx_or_interaction.author
        source_channel = ctx_or_interaction.channel # Used for storing text_channel_id
    elif isinstance(ctx_or_interaction, discord.Interaction):
        user = ctx_or_interaction.user
        source_channel = ctx_or_interaction.channel # Used for storing text_channel_id
    else:
        logger.error("Invalid type passed to ensure_voice")
        return None, "Invalid command source."

    if not guild:
        logger.warning("Ensure_voice called without guild context.")
        return None, "Cannot perform voice actions outside of a server."

    # --- FIX: Define guild_id HERE ---
    guild_id = guild.id

    # Check if source_channel exists (for interactions happening outside text channels?)
    if not source_channel:
        logger.warning(f"[{guild_id}] ensure_voice called without a valid source text channel.")
        # Decide handling: maybe return error, or try finding a default channel? For now, error.
        return None, "Could not determine the text channel for communication."

    if not user.voice:
        return None, f"You are not connected to a voice channel."

    user_voice_channel = user.voice.channel
    voice_client = guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel != user_voice_channel:
            try:
                await voice_client.move_to(user_voice_channel)
                logger.info(f"[{guild_id}] Moved to voice channel: {user_voice_channel.name}")
            except asyncio.TimeoutError:
                logger.error(f"[{guild_id}] Timeout moving to voice channel: {user_voice_channel.name}")
                return None, "Timed out trying to move to your voice channel."
            except Exception as e:
                logger.error(f"[{guild_id}] Error moving voice channel: {e}")
                return None, f"Could not move to your voice channel: {e}"
    else:
        try:
            voice_client = await user_voice_channel.connect(timeout=60.0, reconnect=True) # Added timeout/reconnect
            logger.info(f"[{guild_id}] Connected to voice channel: {user_voice_channel.name}")
        except discord.errors.ClientException as e:
             logger.error(f"[{guild_id}] Error connecting (already connected?): {e}")
             if guild.voice_client: voice_client = guild.voice_client # Try to recover existing client
             else: return None, f"Connection error: {e}. Try `{COMMAND_PREFIX}leave` first."
        except asyncio.TimeoutError:
             logger.error(f"[{guild_id}] Timeout connecting to voice channel: {user_voice_channel.name}")
             return None, "Timed out trying to connect to the voice channel."
        except Exception as e:
            logger.error(f"[{guild_id}] Error connecting to voice channel: {e}", exc_info=True)
            return None, f"Could not connect to the voice channel: {e}"

    # --- Initialize or update guild state ---
    if guild_id not in guild_states:
        guild_states[guild_id] = {} # Initialize if needed

    guild_states[guild_id]['vc'] = voice_client # Store the active VC

    # Store the text channel ID where the command was invoked
    # Use source_channel identified earlier
    guild_states[guild_id]['text_channel_id'] = source_channel.id
    logger.debug(f"[{guild_id}] Stored text channel ID: {source_channel.id}")

    return voice_client, None


async def play_stream(guild_id: int, stream_url: str, stream_name: str, requester: discord.User | discord.Member):
    """Starts playing the stream, setting up state and callbacks."""
    if guild_id not in guild_states or 'vc' not in guild_states[guild_id] or not guild_states[guild_id]['vc']:
        logger.error(f"[{guild_id}] play_stream called without a valid voice client in state.")
        return "Error: Bot is not properly connected to voice."

    voice_client = guild_states[guild_id]['vc']
    if not voice_client.is_connected():
         logger.warning(f"[{guild_id}] play_stream called but voice client disconnected.")
         # Attempt recovery? Or just report error. Reporting is simpler.
         return "Error: Voice client disconnected unexpectedly. Try joining again."

    if not stream_url or not stream_url.startswith(('http://', 'https://')):
        # Basic check, could be more robust (e.g., handle .pls/.m3u indirectly)
        logger.warning(f"[{guild_id}] Invalid stream URL provided or resolved: {stream_url}")
        return "Invalid URL format. Please provide a valid HTTP/HTTPS stream URL."

    # --- Update State ---
    # Ensure text_channel_id is present before proceeding
    if 'text_channel_id' not in guild_states[guild_id] or not guild_states[guild_id]['text_channel_id']:
         logger.error(f"[{guild_id}] play_stream cannot proceed: text_channel_id is missing from state.")
         return "Error: Could not determine the text channel to send updates to."

    guild_states[guild_id].update({
        'url': stream_url,
        'stream_name': stream_name,
        'requester': requester,
        'should_play': True,
        'retries': 0,
        'now_playing_message_id': None # Clear old message ID before playing new stream
    })

    try:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop() # Stop current playback cleanly
            await asyncio.sleep(0.5) # Short delay to ensure stop completes

        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn' # No video
        }
        # Ensure FFmpeg path is correct if not in system PATH
        # audio_source = discord.FFmpegPCMAudio(stream_url, executable="path/to/ffmpeg", **ffmpeg_options)
        audio_source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)

        # Attach the 'after' callback
        after_callback = functools.partial(after_playback_handler, guild_id)
        voice_client.play(audio_source, after=after_callback)

        logger.info(f"[{guild_id}] Started playing stream: {stream_name} ({stream_url}) requested by {requester.name}")

        # Send the Now Playing embed *after* successfully starting play
        # Use create_task to avoid blocking play_stream if sending embed is slow
        asyncio.create_task(send_now_playing_embed(guild_id))

        return f"▶️ Now playing: `{stream_name}`" # Short confirmation text

    except discord.errors.ClientException as e:
        logger.error(f"[{guild_id}] discord.py ClientException during play: {e}")
        guild_states[guild_id]['should_play'] = False
        return f"❌ Discord error playing `{stream_name}`: {e}"
    except Exception as e:
        # Catch potential FFmpeg errors or other issues
        logger.error(f"[{guild_id}] Error starting stream '{stream_name}': {e}", exc_info=True)
        guild_states[guild_id]['should_play'] = False
        return f"❌ Could not play the stream `{stream_name}`. Check URL/logs. Error: {e}"


def after_playback_handler(guild_id: int, error: Exception | None):
    """Callback after playback ends or errors, handles reconnection."""
    if guild_id not in guild_states:
        logger.warning(f"[{guild_id}] after_playback_handler called for unknown guild state.")
        return

    state = guild_states[guild_id]
    should_play = state.get('should_play', False) # Check if we *intended* to stop

    # Schedule embed cleanup using run_coroutine_threadsafe as this runs in a different thread
    cleanup_task = asyncio.run_coroutine_threadsafe(cleanup_now_playing_message(guild_id), bot.loop)
    try:
        cleanup_task.result(timeout=5) # Wait briefly for cleanup task submission/start
    except TimeoutError:
        logger.warning(f"[{guild_id}] Timeout waiting for cleanup task result in 'after' handler.")
    except Exception as e:
        logger.error(f"[{guild_id}] Error submitting cleanup task in 'after' handler: {e}")


    if error:
        logger.error(f"[{guild_id}] Playback Error detected in 'after' callback: {error}")
        if should_play:
            # We were supposed to be playing, so attempt reconnect
            logger.info(f"[{guild_id}] Stream ended with error while should_play=True, attempting reconnect...")
            state['retries'] = state.get('retries', 0) + 1
            if state['retries'] <= MAX_RECONNECT_ATTEMPTS:
                logger.info(f"[{guild_id}] Reconnect attempt {state['retries']}/{MAX_RECONNECT_ATTEMPTS} in {RECONNECT_DELAY}s for URL: {state.get('url')}")

                # Schedule the reconnection attempt asynchronously
                reconnect_task = asyncio.run_coroutine_threadsafe(
                    reconnect_and_play(guild_id, state.get('url'), state.get('stream_name'), state.get('requester')),
                    bot.loop
                )
                try:
                    reconnect_task.result(timeout=5) # Wait briefly for task submission
                except TimeoutError:
                     logger.warning(f"[{guild_id}] Timeout waiting for reconnect task result in 'after' handler.")
                except Exception as e:
                     logger.error(f"[{guild_id}] Error submitting reconnect task in 'after' handler: {e}")
            else:
                logger.warning(f"[{guild_id}] Max reconnect attempts reached for URL: {state.get('url')}. Stopping.")
                state['should_play'] = False # Give up
                # Embed cleanup already scheduled above
        else:
             # Error occurred, but we weren't supposed to be playing (likely stopped manually)
             logger.info(f"[{guild_id}] Playback stopped with error, but manual stop detected (should_play=False). Not reconnecting.")
             # Embed cleanup already scheduled above
    else:
        # Playback finished without error (e.g., stream ended naturally, or stopped manually)
        logger.info(f"[{guild_id}] Playback finished naturally or was stopped.")
        # If stopped manually, should_play is already False. If ended naturally, ensure it's False.
        state['should_play'] = False
        # Embed cleanup already scheduled above


async def reconnect_and_play(guild_id: int, stream_url: str | None, stream_name: str | None, requester: discord.User | discord.Member | None):
    """Attempts to reconnect voice and restart the stream."""
    await asyncio.sleep(RECONNECT_DELAY) # Wait before retrying

    # Double check state after delay
    if guild_id not in guild_states or not guild_states[guild_id].get('should_play', False):
        logger.info(f"[{guild_id}] Reconnect cancelled (should_play became False during delay or state lost).")
        return

    if not stream_url or not stream_name or not requester:
         logger.warning(f"[{guild_id}] Reconnect cancelled (URL, name, or requester missing in state after delay).")
         guild_states[guild_id]['should_play'] = False
         await cleanup_now_playing_message(guild_id) # Ensure cleanup
         return

    logger.info(f"[{guild_id}] Executing reconnect attempt {guild_states[guild_id].get('retries', '?')}...")
    guild = bot.get_guild(guild_id)
    if not guild:
        logger.error(f"[{guild_id}] Guild not found during reconnect attempt.")
        guild_states[guild_id]['should_play'] = False
        return

    # Check Voice Client status
    voice_client = guild_states[guild_id].get('vc')
    if not voice_client or not voice_client.is_connected():
        logger.warning(f"[{guild_id}] Voice client not connected during reconnect attempt. Trying to rejoin...")
        # Rejoining ideally needs the original user's channel, which we don't have easily here.
        # We can only attempt to call connect() on the existing object if it exists.
        if voice_client and hasattr(voice_client, 'channel') and voice_client.channel:
            try:
                await voice_client.connect(timeout=60.0, reconnect=True)
                logger.info(f"[{guild_id}] Reconnected voice client via discord.py method.")
            except Exception as e:
                logger.error(f"[{guild_id}] Failed to reconnect voice client via discord.py method: {e}. Stopping playback attempt.")
                guild_states[guild_id]['should_play'] = False
                await cleanup_now_playing_message(guild_id)
                return
        else:
             logger.error(f"[{guild_id}] No valid voice client or channel found to attempt reconnect. Stopping playback attempt.")
             guild_states[guild_id]['should_play'] = False
             await cleanup_now_playing_message(guild_id)
             return

    # If still supposed to play after delay and potential reconnection attempt
    if guild_states[guild_id].get('should_play', False):
        logger.info(f"[{guild_id}] Retrying play_stream for: {stream_name}")
        # Re-call play_stream with all necessary info
        result_message = await play_stream(guild_id, stream_url, stream_name, requester)
        logger.info(f"[{guild_id}] Re-play attempt result: {result_message}")

        # If the retry itself fails immediately, the 'after' handler of *that* play attempt
        # will trigger and decide if further retries are needed or max attempts reached.
        if "❌" in result_message or "Error:" in result_message:
            logger.warning(f"[{guild_id}] Re-play attempt failed immediately.")
            # No need to explicitly set should_play=False here, 'after_playback_handler' will handle it.


# --- Bot Events ---
@bot.event
async def on_ready():
    """Called when the bot is ready and connected."""
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info(f'Command Prefix: {COMMAND_PREFIX}')
    logger.info(f'Available Predefined Streams: {list(PREDEFINED_STREAMS.keys())}')
    logger.info(f'Initial latency: {bot.latency * 1000:.2f} ms')
    logger.info('------')
    try:
        # Sync slash commands globally. Can take time to propagate.
        # For testing, consider syncing to a specific guild:
        # synced = await bot.tree.sync(guild=discord.Object(id=YOUR_TEST_GUILD_ID))
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} application (slash) command(s).")
    except Exception as e:
        logger.exception(f"Failed to sync slash commands: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle bot disconnection, user leaving, etc."""
    if member.id == bot.user.id and before.channel is not None and after.channel is None:
        # Bot was disconnected from a voice channel
        guild_id = before.channel.guild.id
        logger.info(f"[{guild_id}] Bot disconnected from voice channel '{before.channel.name}'.")
        if guild_id in guild_states:
            guild_states[guild_id]['should_play'] = False # Stop playback attempts
            logger.info(f"[{guild_id}] Playback state reset due to disconnection.")
            # Clean up the Now Playing message using create_task
            asyncio.create_task(cleanup_now_playing_message(guild_id))
            # Optional: Fully clear state: del guild_states[guild_id]
    # Add check if users leave the bot alone in channel? (Optional feature)
    # elif before.channel is not None and bot.user in before.channel.members and len(before.channel.members) == 1:
    #     # Bot is now alone in the channel
    #     guild_id = before.channel.guild.id
    #     if guild_id in guild_states and guild_states[guild_id]['vc']:
    #         logger.info(f"[{guild_id}] Bot is alone in channel '{before.channel.name}', disconnecting.")
    #         await guild_states[guild_id]['vc'].disconnect() # Triggers the above handler


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User | discord.Member):
    """Handle the stop reaction on the Now Playing message."""
    # Ignore reactions from the bot itself or in DMs
    if user.bot or not reaction.message.guild:
        return

    guild_id = reaction.message.guild.id

    # Check if the reaction is the stop emoji and on the currently tracked message
    if str(reaction.emoji) == STOP_REACTION and \
       guild_id in guild_states and \
       reaction.message.id == guild_states[guild_id].get('now_playing_message_id'):

        logger.info(f"[{guild_id}] Stop reaction detected from user {user.name} on message {reaction.message.id}")

        # Check if bot is actually connected and potentially playing
        vc = reaction.message.guild.voice_client
        if vc and vc.is_connected():
            # Set should_play to False *before* stopping
            guild_states[guild_id]['should_play'] = False
            guild_states[guild_id]['url'] = None # Clear URL state
            logger.info(f"[{guild_id}] Stopping playback via reaction.")

            # Stop playback if playing/paused
            if vc.is_playing() or vc.is_paused():
                vc.stop() # Triggers after_playback_handler which cleans up embed

            # Optional: Send a confirmation message
            try:
                # Remove the user's reaction as feedback
                await reaction.remove(user)
                # Send temporary confirmation
                await reaction.message.channel.send(f"⏹️ Playback stopped by {user.mention}.", delete_after=10)
            except discord.Forbidden:
                logger.warning(f"[{guild_id}] Missing permissions to remove reaction or send confirmation message.")
            except Exception as e:
                 logger.error(f"[{guild_id}] Error handling reaction confirmation: {e}")
        else:
            logger.info(f"[{guild_id}] Stop reaction detected, but bot not connected to voice.")
            # Remove reaction if bot isn't playing? Optional.
            # await reaction.remove(user)


# --- Help Command ---

async def send_help_embed(ctx_or_interaction):
    """Sends the help embed."""
    is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
    ephemeral_response = is_interaction # Default to ephemeral for slash commands

    embed = discord.Embed(
        title=f"{bot.user.name} Help",
        description=f"Hi! I'm a simple radio bot. My command prefix is `{COMMAND_PREFIX}`.\nYou can also use slash commands (e.g., `/play`).",
        color=discord.Color.blue()
    )
    try: embed.set_thumbnail(url=bot.user.display_avatar.url)
    except: pass # Ignore if avatar fails

    # Voice Commands
    embed.add_field(
        name="🔊 Voice Commands",
        value=f"""
        `{COMMAND_PREFIX}play <URL or Name>` or `/play stream:<URL or Name>`
        Plays a live radio stream. Use a direct URL or a name from `{COMMAND_PREFIX}list`.

        `{COMMAND_PREFIX}stop` or `/stop`
        Stops the current playback and clears the player.

        `{COMMAND_PREFIX}leave` or `{COMMAND_PREFIX}dc`
        Disconnects the bot from the voice channel. (Stops playback).

        `{COMMAND_PREFIX}now` or `/now`
        Shows the currently playing stream information again.
        """,
        inline=False
    )

    # Utility Commands
    embed.add_field(
        name="ℹ️ Utility Commands",
        value=f"""
        `{COMMAND_PREFIX}help` or `/help`
        Shows this help message.

        `{COMMAND_PREFIX}list` or `/list`
        Shows the predefined radio stream names.

        `{COMMAND_PREFIX}ping`
        Checks the bot's latency.
        """,
        inline=False
    )

    embed.add_field(
        name="▶️ Playback Control",
        value=f"React with {STOP_REACTION} on the 'Now Playing' message to stop playback.",
        inline=False
    )
    embed.set_footer(text="Enjoy the music!")

    # Sending logic
    if isinstance(ctx_or_interaction, commands.Context):
        await ctx_or_interaction.send(embed=embed)
    elif is_interaction:
        try:
            # Try sending ephemeral first for slash commands
            await ctx_or_interaction.response.send_message(embed=embed, ephemeral=ephemeral_response)
        except discord.errors.InteractionResponded:
            # If already responded (e.g., deferred), use followup
            await ctx_or_interaction.followup.send(embed=embed, ephemeral=ephemeral_response)


@bot.command(name='help', help='Shows this help message.')
async def help_prefix(ctx):
    await send_help_embed(ctx)

@bot.tree.command(name="help", description="Shows the bot's help information.")
async def help_slash(interaction: discord.Interaction):
    # No defer needed for potentially ephemeral response
    await send_help_embed(interaction)


# --- List Command ---

async def send_list_embed(ctx_or_interaction):
    """Sends the predefined stream list."""
    is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
    ephemeral_response = is_interaction

    if not PREDEFINED_STREAMS:
        desc = "No predefined streams are currently configured."
    else:
        desc = f"Use these names with `{COMMAND_PREFIX}play <Name>` or `/play stream:<Name>`:"
        stream_list = "\n".join(f"- `{name}`" for name in PREDEFINED_STREAMS.keys())
        desc += f"\n{stream_list}"

    embed = discord.Embed(
        title="📻 Predefined Radio Streams",
        description=desc,
        color=discord.Color.orange()
    )

    if isinstance(ctx_or_interaction, commands.Context):
        await ctx_or_interaction.send(embed=embed)
    elif is_interaction:
        try:
            await ctx_or_interaction.response.send_message(embed=embed, ephemeral=ephemeral_response)
        except discord.errors.InteractionResponded:
            await ctx_or_interaction.followup.send(embed=embed, ephemeral=ephemeral_response)

@bot.command(name='list', help='Shows the list of predefined radio streams.')
async def list_prefix(ctx):
    await send_list_embed(ctx)

@bot.tree.command(name="list", description="Shows the list of predefined radio streams.")
async def list_slash(interaction: discord.Interaction):
    await send_list_embed(interaction)


# --- Standard Commands (Modified for State and Embeds) ---

@bot.command(name='ping', help='Checks bot latency.')
async def ping_prefix(ctx):
    latency = bot.latency * 1000
    await ctx.send(f"Pong! Latency: {latency:.2f} ms")

@bot.command(name='join', help='Makes the bot join your current voice channel.')
async def join_prefix(ctx):
    vc, error_msg = await ensure_voice(ctx)
    if error_msg: await ctx.send(error_msg)
    elif vc: await ctx.send(f"Joined `{vc.channel.name}`.")

@bot.command(name='leave', aliases=['disconnect', 'dc'], help='Makes the bot leave the voice channel.')
async def leave_prefix(ctx):
    guild_id = ctx.guild.id
    vc = ctx.guild.voice_client # Get current VC

    if guild_id in guild_states:
        guild_states[guild_id]['should_play'] = False # Signal intent to stop
        logger.info(f"[{guild_id}] Leave command used, disabling automatic playback.")
        # Explicit cleanup in case bot isn't playing but connected
        asyncio.create_task(cleanup_now_playing_message(guild_id))

    if vc and vc.is_connected():
        channel_name = vc.channel.name
        logger.info(f"[{guild_id}] Disconnecting from voice channel '{channel_name}' via command.")
        await vc.disconnect(force=False) # force=False allows graceful disconnect, triggers on_voice_state_update
        await ctx.send(f"Left `{channel_name}`.")
    else:
        await ctx.send("I'm not currently in a voice channel.")


@bot.command(name='play', aliases=['p', 'stream'], help='Plays a radio stream URL or predefined name.')
async def play_prefix(ctx, *, stream_url_or_name: str):
    guild_id = ctx.guild.id
    # Ensure voice connection and store text channel ID
    voice_client, error_msg = await ensure_voice(ctx)
    if error_msg: await ctx.send(error_msg); return
    if not voice_client: await ctx.send("Could not establish voice connection."); return

    stream_input = stream_url_or_name.strip('<>') # Remove potential accidental angle brackets

    stream_url = stream_input # Assume it's a URL initially
    stream_name = stream_input # Display name

    # Check if input matches a predefined stream name (case-insensitive check)
    matched_name = next((name for name in PREDEFINED_STREAMS if name.lower() == stream_input.lower()), None)
    if matched_name:
        stream_url = PREDEFINED_STREAMS[matched_name]
        stream_name = matched_name # Use the proper case name
        logger.info(f"[{guild_id}] Matched predefined stream: {stream_name}")
    elif not stream_url.startswith(('http://', 'https://')):
        # If not predefined and not a valid URL format
         await ctx.send(f"Input `{stream_input}` is not a valid URL or predefined stream name. See `{COMMAND_PREFIX}list`.")
         return

    # Call the core play function
    result_message = await play_stream(guild_id, stream_url, stream_name, ctx.author)
    # Send simple confirmation text; embed is handled by play_stream
    await ctx.send(result_message)


@bot.command(name='stop', help='Stops the current audio stream.')
async def stop_prefix(ctx):
    guild_id = ctx.guild.id
    vc = ctx.guild.voice_client

    is_playing = vc and (vc.is_playing() or vc.is_paused())

    if guild_id in guild_states:
        guild_states[guild_id]['should_play'] = False # Signal intent to stop
        guild_states[guild_id]['url'] = None
        logger.info(f"[{guild_id}] Stop command used, disabling playback state.")
        # Cleanup task submitted by after_playback handler when vc.stop() is called below,
        # or explicitly if not playing but message exists.
        if not is_playing and guild_states[guild_id].get('now_playing_message_id'):
             asyncio.create_task(cleanup_now_playing_message(guild_id))

    if is_playing:
        logger.info(f"[{guild_id}] Stopping playback via command.")
        vc.stop() # Triggers after_playback_handler
        await ctx.send("⏹️ Playback stopped.")
    elif vc and not is_playing:
         await ctx.send("Nothing is currently playing.")
    else:
        await ctx.send("I'm not connected to a voice channel.")


@bot.command(name='now', aliases=['np'], help='Shows the currently playing stream.')
async def now_prefix(ctx):
    guild_id = ctx.guild.id
    if guild_id in guild_states and guild_states[guild_id].get('should_play') and guild_states[guild_id].get('vc') and guild_states[guild_id]['vc'].is_playing():
        # Resend the embed - ensures it's up-to-date and visible
        logger.info(f"[{guild_id}] Resending Now Playing embed via command.")
        await send_now_playing_embed(guild_id)
        try:
            # Delete the user's command message for cleanliness
             await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass # Ignore if delete fails
    else:
        await ctx.send("Not currently playing anything.")


# --- Slash Commands (Mirrored) ---

# Using @discord.app_commands.rename for better argument display name
@bot.tree.command(name="play", description="Plays a radio stream URL or predefined name.")
@discord.app_commands.describe(stream="The URL or predefined name of the stream (see /list)")
async def play_slash(interaction: discord.Interaction, stream: str):
    # Defer response first
    try:
        await interaction.response.defer()
    except discord.errors.InteractionResponded:
        logger.warning(f"[{interaction.guild_id}] Play interaction already responded.")
    except Exception as e:
        logger.error(f"[{interaction.guild_id}] Defer failed for play command: {e}")
        # Try to notify user if possible
        try: await interaction.followup.send("Error: Could not process command.", ephemeral=True)
        except: pass
        return

    guild_id = interaction.guild_id
    # Ensure voice connection and store text channel ID
    voice_client, error_msg = await ensure_voice(interaction)
    if error_msg: await interaction.followup.send(error_msg); return
    if not voice_client: await interaction.followup.send("Could not establish voice connection."); return

    stream_input = stream.strip('<>')

    stream_url = stream_input
    stream_name = stream_input

    matched_name = next((name for name in PREDEFINED_STREAMS if name.lower() == stream_input.lower()), None)
    if matched_name:
        stream_url = PREDEFINED_STREAMS[matched_name]
        stream_name = matched_name
        logger.info(f"[{guild_id}] Matched predefined stream (slash): {stream_name}")
    elif not stream_url.startswith(('http://', 'https://')):
         await interaction.followup.send(f"Input `{stream_input}` is not a valid URL or predefined stream name. See `/list`.", ephemeral=True)
         return

    # Call the core play function
    result_message = await play_stream(guild_id, stream_url, stream_name, interaction.user)
    # Send simple confirmation text; embed is handled by play_stream
    await interaction.followup.send(result_message)


@bot.tree.command(name="stop", description="Stops the current audio stream.")
async def stop_slash(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except discord.errors.InteractionResponded:
         logger.warning(f"[{interaction.guild_id}] Stop interaction already responded.")
    except Exception as e:
        logger.error(f"[{interaction.guild_id}] Defer failed for stop command: {e}")
        try: await interaction.followup.send("Error: Could not process command.", ephemeral=True)
        except: pass
        return

    guild_id = interaction.guild_id
    vc = interaction.guild.voice_client if interaction.guild else None
    is_playing = vc and (vc.is_playing() or vc.is_paused())

    if guild_id in guild_states:
        guild_states[guild_id]['should_play'] = False
        guild_states[guild_id]['url'] = None
        logger.info(f"[{guild_id}] Stop command used (slash), disabling playback state.")
        if not is_playing and guild_states[guild_id].get('now_playing_message_id'):
             asyncio.create_task(cleanup_now_playing_message(guild_id))

    if is_playing:
        logger.info(f"[{guild_id}] Stopping playback via slash command.")
        vc.stop() # Triggers after_playback_handler
        await interaction.followup.send("⏹️ Playback stopped.")
    elif vc and not is_playing:
         await interaction.followup.send("Nothing is currently playing.", ephemeral=True)
    else:
        await interaction.followup.send("I'm not connected to a voice channel.", ephemeral=True)


@bot.tree.command(name="now", description="Shows the currently playing stream.")
async def now_slash(interaction: discord.Interaction):
    # Defer ephemerally first
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.InteractionResponded:
         logger.warning(f"[{interaction.guild_id}] Now interaction already responded.")
    except Exception as e:
        logger.error(f"[{interaction.guild_id}] Defer failed for now command: {e}")
        try: await interaction.followup.send("Error: Could not process command.", ephemeral=True)
        except: pass
        return

    guild_id = interaction.guild_id
    if guild_id in guild_states and \
       guild_states[guild_id].get('should_play') and \
       guild_states[guild_id].get('vc') and \
       guild_states[guild_id]['vc'].is_playing():
        # Resend the main embed (will be visible to everyone)
        logger.info(f"[{guild_id}] Resending Now Playing embed via slash command.")
        await send_now_playing_embed(guild_id)
        # Send ephemeral confirmation to the user who asked
        await interaction.followup.send("Current stream info displayed.", ephemeral=True)
    else:
        await interaction.followup.send("Not currently playing anything.", ephemeral=True)


# --- Error Handling ---
@bot.event
async def on_command_error(ctx, error):
    """Handles errors for prefix commands."""
    if isinstance(error, commands.CommandNotFound):
        # Silently ignore commands not found
        pass
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. See `{COMMAND_PREFIX}help`.", delete_after=15)
    elif isinstance(error, commands.CommandInvokeError):
        original = error.original
        logger.error(f"Error in prefix command '{ctx.command}': {original}", exc_info=original)
        await ctx.send(f"An error occurred running `{ctx.command}`: ```{original}```")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("You don't have permission to use this command.", delete_after=15)
    else:
        logger.error(f"Unhandled prefix command error for '{ctx.command}': {error}", exc_info=error)
        # Avoid sending generic error message for unknown types unless needed


# --- Global Slash Command Error Handler (Optional but recommended) ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    """Handles errors specifically for slash commands."""
    if isinstance(error, discord.app_commands.CommandInvokeError):
        original_error = error.original
        logger.error(f"Error in slash command '{interaction.command.name if interaction.command else 'Unknown'}': {original_error}", exc_info=original_error)
        error_message = f"An internal error occurred: ```{original_error}```"
    elif isinstance(error, discord.app_commands.CheckFailure):
        logger.warning(f"Check failed for slash command '{interaction.command.name}' by user {interaction.user}: {error}")
        error_message = "You don't have the necessary permissions or context to use this command."
    # Add more specific AppCommandError types as needed
    # discord.app_commands.CommandNotFound # Should typically not happen with synced commands
    # discord.app_commands.TransformerError # For bad argument types
    # discord.app_commands.CommandOnCooldown
    else:
        logger.error(f"Unhandled slash command error for '{interaction.command.name if interaction.command else 'Unknown'}': {error}", exc_info=error)
        error_message = "An unexpected error occurred while processing the command."

    # Try to respond ephemerally
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(error_message, ephemeral=True)
        else:
            await interaction.followup.send(error_message, ephemeral=True)
    except discord.NotFound:
        logger.warning(f"[{interaction.guild_id}] Interaction expired before error could be sent for command '{interaction.command.name}'.")
    except discord.Forbidden:
         logger.warning(f"[{interaction.guild_id}] Missing permissions to send error message for command '{interaction.command.name}'.")
    except Exception as e:
        logger.error(f"[{interaction.guild_id}] Failed to send error message itself for command '{interaction.command.name}': {e}")


# --- Run the Bot ---
if __name__ == "__main__":
    if not BOT_TOKEN: # Check if token is None or empty
        logger.critical("CRITICAL ERROR: DISCORD_TOKEN environment variable not set or empty.")
    else:
        try:
            # Run the bot. log_handler=None prevents discord.py from overriding our basicConfig.
            bot.run(BOT_TOKEN, log_handler=None)
        except discord.errors.LoginFailure:
            logger.critical("CRITICAL ERROR: Login Failed - Improper token provided.")
        except discord.errors.PrivilegedIntentsRequired as e:
             logger.critical(f"CRITICAL ERROR: Privileged Intents ({e.shard_id}) are required but not enabled in the Developer Portal! Check Message Content, Reactions, etc.")
        except Exception as e:
            # Catch other potential startup errors
            logger.critical(f"CRITICAL ERROR: Failed to run bot - {e}", exc_info=True)
