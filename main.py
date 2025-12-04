import discord
from discord.ext import commands, tasks
import logging
from dotenv import load_dotenv
import os
import aiohttp
from datetime import datetime, timedelta
import asyncio
from flask import Flask
from threading import Thread

load_dotenv()
token = os.getenv('DISCORD_TOKEN')
ESPN_API_KEY = os.getenv('ESPN_API_KEY', '')  # Optional

UPDATE_CHANNEL_ID = int(os.getenv('UPDATE_CHANNEL_ID', 1444925754457460819))

# Flask app for health checks (keeps Render from sleeping)
app = Flask(__name__)


@app.route('/')
def home():
    return "Sports Bot is running!", 200


@app.route('/health')
def health():
    return {"status": "healthy", "bot": bot.user.name if bot.is_ready() else "starting"}, 200


def run_flask():
    """Run Flask app in a separate thread"""
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port)


handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

if not token:
    raise ValueError("DISCORD_TOKEN not found in environment variables")

bot = commands.Bot(command_prefix=';', intents=intents, help_command=None)

# Store game states to track changes
game_states = {}
tracked_teams = {}  # Store team filters per server
injury_cache = {}  # Cache injury reports

# API endpoints for various sports
SPORT_APIS = {
    'nba': 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    'nfl': 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
    'nhl': 'https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard',
    'ncaab': 'https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard',
    'ncaaf': 'https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard',
    'mma': 'https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard',
    'tennis': 'https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard',
    'wtennis': 'https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard'
}


class SportsTracker:
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        self.tracked_sports = set()
        self.last_injury_check = {}

    async def fetch_data(self, url):
        """Fetch data from API"""
        if not self.session:
            self.session = aiohttp.ClientSession()

        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"Error fetching data: {response.status}")
                    return None
        except Exception as e:
            print(f"Exception fetching data: {e}")
            return None

    async def fetch_team_info(self, sport, team_id):
        """Fetch detailed team information including injuries"""
        team_url = f"https://site.api.espn.com/apis/site/v2/sports/{self.get_sport_path(sport)}/teams/{team_id}"
        return await self.fetch_data(team_url)

    def get_sport_path(self, sport):
        """Get the API path for a sport"""
        paths = {
            'nba': 'basketball/nba',
            'nfl': 'football/nfl',
            'nhl': 'hockey/nhl',
            'ncaab': 'basketball/mens-college-basketball',
            'ncaaf': 'football/college-football',
            'mma': 'mma/ufc',
            'tennis': 'tennis/atp',
            'wtennis': 'tennis/wta'
        }
        return paths.get(sport, '')

    def create_game_embed(self, game, sport, guild_id=None):
        """Create a Discord embed for a game"""
        competition = game.get('competitions', [{}])[0]
        competitors = competition.get('competitors', [])

        if len(competitors) < 2:
            return None

        home_team = next((team for team in competitors if team.get('homeAway') == 'home'), {})
        away_team = next((team for team in competitors if team.get('homeAway') == 'away'), {})

        # Check team filter if guild_id provided
        if guild_id and guild_id in tracked_teams:
            team_filters = tracked_teams[guild_id]
            home_name = home_team.get('team', {}).get('displayName', '').lower()
            away_name = away_team.get('team', {}).get('displayName', '').lower()

            match_found = False
            for team_filter in team_filters:
                if team_filter in home_name or team_filter in away_name:
                    match_found = True
                    break

            if not match_found:
                return None

        home_score = home_team.get('score', '0')
        away_score = away_team.get('score', '0')
        home_name = home_team.get('team', {}).get('displayName', 'Unknown')
        away_name = away_team.get('team', {}).get('displayName', 'Unknown')

        status = game.get('status', {})
        status_type = status.get('type', {}).get('name', 'Unknown')
        status_detail = status.get('type', {}).get('detail', '')

        # Determine embed color based on game status
        if status_type == 'STATUS_IN_PROGRESS':
            color = discord.Color.green()
        elif status_type == 'STATUS_FINAL':
            color = discord.Color.red()
        else:
            color = discord.Color.blue()

        embed = discord.Embed(
            title=f"{away_name} @ {home_name}",
            color=color,
            timestamp=datetime.utcnow()
        )

        embed.add_field(name="Score", value=f"{away_name}: **{away_score}**\n{home_name}: **{home_score}**",
                        inline=False)
        embed.add_field(name="Status", value=status_detail, inline=False)

        # Add possession indicator for football
        if sport in ['nfl', 'ncaaf']:
            if home_team.get('possession'):
                embed.add_field(name="Possession", value=f"🏈 {home_name}", inline=True)
            elif away_team.get('possession'):
                embed.add_field(name="Possession", value=f"🏈 {away_name}", inline=True)

        # Add period/quarter info
        period = status.get('period', 0)
        if period > 0:
            period_name = self.get_period_name(sport, period)
            embed.add_field(name="Period", value=period_name, inline=True)

        # Add statistics if available
        if status_type == 'STATUS_IN_PROGRESS' or status_type == 'STATUS_FINAL':
            stats_added = self.add_game_stats(embed, competition, sport)

        embed.set_footer(text=f"{sport.upper()} | {status_type}")

        return embed

    def add_game_stats(self, embed, competition, sport):
        """Add game statistics to embed"""
        competitors = competition.get('competitors', [])
        if len(competitors) < 2:
            return False

        home_team = next((team for team in competitors if team.get('homeAway') == 'home'), {})
        away_team = next((team for team in competitors if team.get('homeAway') == 'away'), {})

        home_stats = home_team.get('statistics', [])
        away_stats = away_team.get('statistics', [])

        # Add key stats based on sport
        if sport in ['nfl', 'ncaaf']:
            # Total yards, turnovers
            for stat in home_stats:
                if stat.get('name') == 'totalYards':
                    home_yards = stat.get('displayValue', '0')
                if stat.get('name') == 'turnovers':
                    home_turnovers = stat.get('displayValue', '0')

            for stat in away_stats:
                if stat.get('name') == 'totalYards':
                    away_yards = stat.get('displayValue', '0')
                if stat.get('name') == 'turnovers':
                    away_turnovers = stat.get('displayValue', '0')

            if 'home_yards' in locals():
                embed.add_field(name="Total Yards", value=f"Away: {away_yards}\nHome: {home_yards}", inline=True)
            if 'home_turnovers' in locals():
                embed.add_field(name="Turnovers", value=f"Away: {away_turnovers}\nHome: {home_turnovers}", inline=True)

        return True

    def get_period_name(self, sport, period):
        """Get the proper name for the period based on sport"""
        if sport in ['nba', 'ncaab']:
            if period <= 4:
                return f"{period}Q"
            else:
                return f"OT{period - 4}"
        elif sport in ['nfl', 'ncaaf']:
            if period <= 4:
                return f"Q{period}"
            else:
                return f"OT{period - 4}"
        elif sport == 'nhl':
            if period <= 3:
                return f"P{period}"
            else:
                return "OT"
        else:
            return f"Period {period}"

    def detect_score_change(self, game_id, new_score, old_state):
        """Detect if score has changed"""
        if game_id not in old_state:
            return False

        old_score = old_state[game_id].get('score', '')
        return new_score != old_score

    def detect_game_start(self, game_id, status, old_state):
        """Detect if game has started"""
        if game_id not in old_state:
            return status == 'STATUS_IN_PROGRESS'

        old_status = old_state[game_id].get('status', '')
        return old_status != 'STATUS_IN_PROGRESS' and status == 'STATUS_IN_PROGRESS'

    def detect_game_end(self, game_id, status, old_state):
        """Detect if game has ended"""
        if game_id not in old_state:
            return False

        old_status = old_state[game_id].get('status', '')
        return old_status == 'STATUS_IN_PROGRESS' and status == 'STATUS_FINAL'

    async def check_sport_updates(self, sport):
        """Check for updates in a specific sport"""
        channel = self.bot.get_channel(UPDATE_CHANNEL_ID)
        if not channel:
            return

        guild_id = channel.guild.id if channel.guild else None

        url = SPORT_APIS.get(sport)
        if not url:
            return

        data = await self.fetch_data(url)
        if not data:
            return

        events = data.get('events', [])

        for game in events:
            game_id = game.get('id')
            competition = game.get('competitions', [{}])[0]
            competitors = competition.get('competitors', [])

            if len(competitors) < 2:
                continue

            home_team = next((team for team in competitors if team.get('homeAway') == 'home'), {})
            away_team = next((team for team in competitors if team.get('homeAway') == 'away'), {})

            current_score = f"{away_team.get('score', '0')}-{home_team.get('score', '0')}"
            status = game.get('status', {}).get('type', {}).get('name', '')

            # Check for game start
            if self.detect_game_start(game_id, status, game_states):
                embed = self.create_game_embed(game, sport, guild_id)
                if embed:
                    embed.title = f"🏁 GAME STARTED: {embed.title}"
                    await channel.send(embed=embed)

            # Check for score change
            elif self.detect_score_change(game_id, current_score, game_states):
                embed = self.create_game_embed(game, sport, guild_id)
                if embed:
                    embed.title = f"⚡ SCORE UPDATE: {embed.title}"
                    await channel.send(embed=embed)

            # Check for game end
            elif self.detect_game_end(game_id, status, game_states):
                embed = self.create_game_embed(game, sport, guild_id)
                if embed:
                    embed.title = f"🏆 FINAL: {embed.title}"

                    # Determine winner
                    home_score = int(home_team.get('score', 0))
                    away_score = int(away_team.get('score', 0))
                    if home_score > away_score:
                        winner = home_team.get('team', {}).get('displayName', 'Home')
                    else:
                        winner = away_team.get('team', {}).get('displayName', 'Away')

                    embed.add_field(name="Winner", value=f"🎉 {winner}", inline=False)
                    await channel.send(embed=embed)

            # Update game state
            game_states[game_id] = {
                'score': current_score,
                'status': status,
                'last_update': datetime.utcnow()
            }

    async def close(self):
        """Close the aiohttp session"""
        if self.session:
            await self.session.close()


# Initialize tracker
tracker = SportsTracker(bot)


@tasks.loop(seconds=30)
async def update_sports():
    """Check for updates every 30 seconds"""
    for sport in tracker.tracked_sports:
        await tracker.check_sport_updates(sport)


@bot.command()
async def track(ctx, sport: str):
    """Start tracking a sport (nba, nfl, nhl, ncaab, ncaaf, mma)"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    tracker.tracked_sports.add(sport)
    await ctx.send(f"✅ Now tracking {sport.upper()} updates!")

    if not update_sports.is_running():
        update_sports.start()


@bot.command()
async def untrack(ctx, sport: str):
    """Stop tracking a sport"""
    sport = sport.lower()
    if sport in tracker.tracked_sports:
        tracker.tracked_sports.remove(sport)
        await ctx.send(f"✅ Stopped tracking {sport.upper()} updates.")
    else:
        await ctx.send(f"❌ Not currently tracking {sport.upper()}.")


@bot.command()
async def tracking(ctx):
    """Show currently tracked sports"""
    if tracker.tracked_sports:
        sports_list = ", ".join([s.upper() for s in tracker.tracked_sports])
        await ctx.send(f"📊 Currently tracking: {sports_list}")
    else:
        await ctx.send("📊 Not tracking any sports. Use `;track <sport>` to start!")


@bot.command()
async def scores(ctx, sport: str):
    """Get current scores for a sport"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"🔍 Fetching {sport.upper()} scores...")

    data = await tracker.fetch_data(SPORT_APIS[sport])
    if not data:
        await ctx.send("❌ Failed to fetch data.")
        return

    events = data.get('events', [])
    if not events:
        await ctx.send(f"📭 No games found for {sport.upper()} today.")
        return

    for game in events[:5]:  # Limit to 5 games to avoid spam
        embed = tracker.create_game_embed(game, sport)
        if embed:
            await ctx.send(embed=embed)


@bot.command()
async def help(ctx):
    """Display all available commands"""
    embed = discord.Embed(
        title="🏆 Sports Bot Commands",
        description="Track live sports updates across multiple leagues!",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="📊 Tracking Commands",
        value=(
            "`;track <sport>` - Start tracking a sport\n"
            "`;untrack <sport>` - Stop tracking a sport\n"
            "`;tracking` - Show tracked sports\n"
            "`;filterteam <team>` - Only show games for specific team\n"
            "`;clearfilter` - Remove team filters\n"
            "`;listfilters` - Show active team filters"
        ),
        inline=False
    )

    embed.add_field(
        name="📈 Score Commands",
        value=(
            "`;scores <sport>` - Get current scores\n"
            "`;schedule <sport>` - View upcoming games\n"
            "`;stats <sport> <team>` - Get team statistics\n"
            "`;standings <sport>` - View league standings"
        ),
        inline=False
    )

    embed.add_field(
        name="🏥 Injury & Player Commands",
        value=(
            "`;injuries <sport>` - View injury reports\n"
            "`;teaminjuries <sport> <team>` - Team-specific injuries\n"
            "`;player <sport> <player>` - Player information"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Utility Commands",
        value=(
            "`;ping` - Check bot latency\n"
            "`;setchannel` - Set update channel (Admin)\n"
            "`;help` - Show this message"
        ),
        inline=False
    )

    embed.add_field(
        name="🏅 Available Sports",
        value="`nba`, `nfl`, `nhl`, `ncaab`, `ncaaf`, `mma`, `tennis`, `wtennis`",
        inline=False
    )

    embed.set_footer(text="Use ; as prefix for all commands")

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx):
    """Set the current channel as the update channel"""
    global UPDATE_CHANNEL_ID
    UPDATE_CHANNEL_ID = ctx.channel.id
    await ctx.send(f"✅ Update channel set to {ctx.channel.mention}")


@bot.command()
async def filterteam(ctx, *, team_name: str):
    """Filter updates to only show specific team(s)"""
    guild_id = ctx.guild.id
    if guild_id not in tracked_teams:
        tracked_teams[guild_id] = []

    team_name_lower = team_name.lower()
    if team_name_lower not in tracked_teams[guild_id]:
        tracked_teams[guild_id].append(team_name_lower)
        await ctx.send(f"✅ Now filtering for team: **{team_name}**")
    else:
        await ctx.send(f"⚠️ Already filtering for: **{team_name}**")


@bot.command()
async def clearfilter(ctx):
    """Clear all team filters"""
    guild_id = ctx.guild.id
    if guild_id in tracked_teams:
        tracked_teams[guild_id] = []
        await ctx.send("✅ All team filters cleared!")
    else:
        await ctx.send("⚠️ No filters to clear.")


@bot.command()
async def listfilters(ctx):
    """List active team filters"""
    guild_id = ctx.guild.id
    if guild_id in tracked_teams and tracked_teams[guild_id]:
        teams = ", ".join([t.title() for t in tracked_teams[guild_id]])
        await ctx.send(f"📋 Active filters: {teams}")
    else:
        await ctx.send("📋 No active team filters.")


@bot.command()
async def schedule(ctx, sport: str, days: int = 7):
    """Get upcoming schedule for a sport"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"📅 Fetching {sport.upper()} schedule for next {days} days...")

    # Fetch schedule with date parameter
    base_url = SPORT_APIS[sport]

    data = await tracker.fetch_data(base_url)
    if not data:
        await ctx.send("❌ Failed to fetch schedule.")
        return

    events = data.get('events', [])
    upcoming = []

    for game in events:
        status = game.get('status', {}).get('type', {}).get('name', '')
        if status == 'STATUS_SCHEDULED':
            upcoming.append(game)

    if not upcoming:
        await ctx.send(f"📭 No upcoming games found for {sport.upper()}.")
        return

    embed = discord.Embed(
        title=f"📅 {sport.upper()} Upcoming Schedule",
        color=discord.Color.blue()
    )

    for game in upcoming[:10]:  # Show up to 10 games
        competition = game.get('competitions', [{}])[0]
        competitors = competition.get('competitors', [])

        if len(competitors) >= 2:
            home_team = next((team for team in competitors if team.get('homeAway') == 'home'), {})
            away_team = next((team for team in competitors if team.get('homeAway') == 'away'), {})

            home_name = home_team.get('team', {}).get('displayName', 'Unknown')
            away_name = away_team.get('team', {}).get('displayName', 'Unknown')

            date_str = game.get('date', '')
            if date_str:
                game_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                formatted_date = game_date.strftime("%m/%d %I:%M %p")
            else:
                formatted_date = "TBA"

            embed.add_field(
                name=f"{away_name} @ {home_name}",
                value=f"🕐 {formatted_date}",
                inline=False
            )

    await ctx.send(embed=embed)


@bot.command()
async def injuries(ctx, sport: str):
    """Get injury report for a sport"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"🏥 Fetching {sport.upper()} injury reports...")

    # Get today's games to find teams
    data = await tracker.fetch_data(SPORT_APIS[sport])
    if not data:
        await ctx.send("❌ Failed to fetch data.")
        return

    embed = discord.Embed(
        title=f"🏥 {sport.upper()} Injury Report",
        description="Recent injury updates",
        color=discord.Color.orange()
    )

    events = data.get('events', [])
    injury_count = 0

    for game in events[:5]:  # Check first 5 games
        competition = game.get('competitions', [{}])[0]
        competitors = competition.get('competitors', [])

        for competitor in competitors:
            team_name = competitor.get('team', {}).get('displayName', 'Unknown')
            injuries_list = competitor.get('injuries', [])

            if injuries_list:
                injury_text = []
                for injury in injuries_list[:3]:  # Limit to 3 per team
                    player_name = injury.get('athlete', {}).get('displayName', 'Unknown')
                    status = injury.get('status', 'Unknown')
                    injury_type = injury.get('details', {}).get('type', 'Injury')
                    injury_text.append(f"• {player_name} - {status} ({injury_type})")
                    injury_count += 1

                if injury_text:
                    embed.add_field(
                        name=f"{team_name}",
                        value="\n".join(injury_text),
                        inline=False
                    )

    if injury_count == 0:
        embed.description = "No injuries reported at this time."

    await ctx.send(embed=embed)


@bot.command()
async def teaminjuries(ctx, sport: str, *, team_name: str):
    """Get injuries for a specific team"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"🔍 Searching for {team_name} injuries...")

    data = await tracker.fetch_data(SPORT_APIS[sport])
    if not data:
        await ctx.send("❌ Failed to fetch data.")
        return

    events = data.get('events', [])
    team_found = False

    for game in events:
        competition = game.get('competitions', [{}])[0]
        competitors = competition.get('competitors', [])

        for competitor in competitors:
            current_team = competitor.get('team', {}).get('displayName', '').lower()
            if team_name.lower() in current_team:
                team_found = True
                team_display = competitor.get('team', {}).get('displayName', 'Unknown')
                injuries_list = competitor.get('injuries', [])

                embed = discord.Embed(
                    title=f"🏥 {team_display} Injury Report",
                    color=discord.Color.orange()
                )

                if not injuries_list:
                    embed.description = "No injuries reported."
                else:
                    for injury in injuries_list:
                        player_name = injury.get('athlete', {}).get('displayName', 'Unknown')
                        status = injury.get('status', 'Unknown')
                        injury_type = injury.get('details', {}).get('type', 'Injury')

                        embed.add_field(
                            name=player_name,
                            value=f"**Status:** {status}\n**Type:** {injury_type}",
                            inline=True
                        )

                await ctx.send(embed=embed)
                break

        if team_found:
            break

    if not team_found:
        await ctx.send(f"❌ Team '{team_name}' not found in today's games.")


@bot.command()
async def stats(ctx, sport: str, *, team_name: str):
    """Get team statistics"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"📊 Fetching statistics for {team_name}...")

    data = await tracker.fetch_data(SPORT_APIS[sport])
    if not data:
        await ctx.send("❌ Failed to fetch data.")
        return

    events = data.get('events', [])
    team_found = False

    for game in events:
        competition = game.get('competitions', [{}])[0]
        competitors = competition.get('competitors', [])

        for competitor in competitors:
            current_team = competitor.get('team', {}).get('displayName', '').lower()
            if team_name.lower() in current_team:
                team_found = True
                team_display = competitor.get('team', {}).get('displayName', 'Unknown')
                team_record = competitor.get('records', [{}])[0].get('summary', 'N/A')
                statistics = competitor.get('statistics', [])

                embed = discord.Embed(
                    title=f"📊 {team_display} Statistics",
                    description=f"**Record:** {team_record}",
                    color=discord.Color.blue()
                )

                if statistics:
                    for stat in statistics[:8]:  # Show top 8 stats
                        stat_name = stat.get('displayName', stat.get('name', 'Unknown'))
                        stat_value = stat.get('displayValue', 'N/A')
                        embed.add_field(name=stat_name, value=stat_value, inline=True)
                else:
                    embed.add_field(name="Stats", value="No statistics available", inline=False)

                await ctx.send(embed=embed)
                break

        if team_found:
            break

    if not team_found:
        await ctx.send(f"❌ Team '{team_name}' not found in recent games.")


@bot.command()
async def standings(ctx, sport: str):
    """Get league standings"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"📊 Fetching {sport.upper()} standings...")

    # Construct standings URL
    sport_path = tracker.get_sport_path(sport)
    standings_url = f"https://site.api.espn.com/apis/v2/sports/{sport_path}/standings"

    data = await tracker.fetch_data(standings_url)
    if not data:
        await ctx.send("❌ Failed to fetch standings.")
        return

    embed = discord.Embed(
        title=f"🏆 {sport.upper()} Standings",
        color=discord.Color.gold()
    )

    children = data.get('children', [])

    if not children:
        await ctx.send("❌ No standings data available.")
        return

    for conference in children[:2]:  # Show top 2 conferences/divisions
        conf_name = conference.get('name', 'Unknown')
        standings = conference.get('standings', {}).get('entries', [])

        standings_text = []
        for i, entry in enumerate(standings[:10], 1):  # Top 10 teams
            team = entry.get('team', {})
            team_name = team.get('displayName', 'Unknown')
            stats = entry.get('stats', [])

            wins = next((s.get('value', 0) for s in stats if s.get('name') == 'wins'), 0)
            losses = next((s.get('value', 0) for s in stats if s.get('name') == 'losses'), 0)

            standings_text.append(f"{i}. {team_name} ({int(wins)}-{int(losses)})")

        if standings_text:
            embed.add_field(
                name=conf_name,
                value="\n".join(standings_text),
                inline=False
            )

    await ctx.send(embed=embed)


@bot.command()
async def player(ctx, sport: str, *, player_name: str):
    """Get player information"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"🔍 Searching for player: {player_name}...")

    # ESPN player search URL
    sport_path = tracker.get_sport_path(sport)
    search_url = f"https://site.api.espn.com/apis/common/v3/search?query={player_name.replace(' ', '%20')}&type=player"

    data = await tracker.fetch_data(search_url)
    if not data:
        await ctx.send("❌ Failed to search for player.")
        return

    results = data.get('results', [])

    if not results:
        await ctx.send(f"❌ No player found matching '{player_name}'.")
        return

    # Get first result
    player = results[0]

    embed = discord.Embed(
        title=f"👤 {player.get('displayName', 'Unknown')}",
        description=player.get('description', 'No description available'),
        color=discord.Color.purple()
    )

    if player.get('image'):
        embed.set_thumbnail(url=player.get('image'))

    # Add available info
    if player.get('teamName'):
        embed.add_field(name="Team", value=player.get('teamName'), inline=True)

    if player.get('position'):
        embed.add_field(name="Position", value=player.get('position'), inline=True)

    if player.get('jersey'):
        embed.add_field(name="Jersey", value=f"#{player.get('jersey')}", inline=True)

    await ctx.send(embed=embed)


@bot.command()
async def livescore(ctx, sport: str, *, search_term: str = None):
    """Get live scores with auto-updates for a specific sport or team"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    data = await tracker.fetch_data(SPORT_APIS[sport])
    if not data:
        await ctx.send("❌ Failed to fetch data.")
        return

    events = data.get('events', [])
    live_games = []

    for game in events:
        status = game.get('status', {}).get('type', {}).get('name', '')
        if status == 'STATUS_IN_PROGRESS':
            # If search term provided, filter by team name
            if search_term:
                competition = game.get('competitions', [{}])[0]
                competitors = competition.get('competitors', [])
                team_names = ' '.join([c.get('team', {}).get('displayName', '').lower() for c in competitors])
                if search_term.lower() not in team_names:
                    continue
            live_games.append(game)

    if not live_games:
        await ctx.send(
            f"📭 No live games found for {sport.upper()}" + (f" matching '{search_term}'" if search_term else ""))
        return

    # Send initial scores
    embed = discord.Embed(
        title=f"🔴 LIVE: {sport.upper()} Scores",
        description=f"Live updates • Refreshing every 30 seconds",
        color=discord.Color.green()
    )

    for game in live_games[:5]:
        competition = game.get('competitions', [{}])[0]
        competitors = competition.get('competitors', [])

        if len(competitors) >= 2:
            home_team = next((team for team in competitors if team.get('homeAway') == 'home'), {})
            away_team = next((team for team in competitors if team.get('homeAway') == 'away'), {})

            home_name = home_team.get('team', {}).get('displayName', 'Unknown')
            away_name = away_team.get('team', {}).get('displayName', 'Unknown')
            home_score = home_team.get('score', '0')
            away_score = away_team.get('score', '0')

            status_detail = game.get('status', {}).get('type', {}).get('detail', 'Live')

            embed.add_field(
                name=f"{away_name} @ {home_name}",
                value=f"**{away_score} - {home_score}**\n{status_detail}",
                inline=False
            )

    message = await ctx.send(embed=embed)

    # Update scores every 30 seconds for 5 minutes
    for _ in range(10):
        await asyncio.sleep(30)

        # Fetch new data
        data = await tracker.fetch_data(SPORT_APIS[sport])
        if not data:
            break

        # Update embed with new scores
        embed.clear_fields()
        embed.timestamp = datetime.utcnow()

        events = data.get('events', [])
        updated_count = 0

        for game in events:
            status = game.get('status', {}).get('type', {}).get('name', '')
            if status == 'STATUS_IN_PROGRESS':
                if search_term:
                    competition = game.get('competitions', [{}])[0]
                    competitors = competition.get('competitors', [])
                    team_names = ' '.join([c.get('team', {}).get('displayName', '').lower() for c in competitors])
                    if search_term.lower() not in team_names:
                        continue

                competition = game.get('competitions', [{}])[0]
                competitors = competition.get('competitors', [])

                if len(competitors) >= 2:
                    home_team = next((team for team in competitors if team.get('homeAway') == 'home'), {})
                    away_team = next((team for team in competitors if team.get('homeAway') == 'away'), {})

                    home_name = home_team.get('team', {}).get('displayName', 'Unknown')
                    away_name = away_team.get('team', {}).get('displayName', 'Unknown')
                    home_score = home_team.get('score', '0')
                    away_score = away_team.get('score', '0')

                    status_detail = game.get('status', {}).get('type', {}).get('detail', 'Live')

                    embed.add_field(
                        name=f"{away_name} @ {home_name}",
                        value=f"**{away_score} - {home_score}**\n{status_detail}",
                        inline=False
                    )
                    updated_count += 1

                    if updated_count >= 5:
                        break

        if updated_count == 0:
            embed.description = "All games have ended or no live games found"
            await message.edit(embed=embed)
            break

        try:
            await message.edit(embed=embed)
        except:
            break


@bot.command()
async def playerstats(ctx, sport: str, *, player_name: str):
    """Get live/recent stats for a specific player"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    await ctx.send(f"🔍 Searching for {player_name}'s stats...")

    # Get today's games
    data = await tracker.fetch_data(SPORT_APIS[sport])
    if not data:
        await ctx.send("❌ Failed to fetch data.")
        return

    events = data.get('events', [])
    player_found = False

    for game in events:
        competition = game.get('competitions', [{}])[0]
        competitors = competition.get('competitors', [])

        for competitor in competitors:
            # Look for player in team roster/stats
            athletes = competitor.get('athletes', [])

            for athlete in athletes:
                athlete_name = athlete.get('displayName', '').lower()
                if player_name.lower() in athlete_name:
                    player_found = True

                    embed = discord.Embed(
                        title=f"📊 {athlete.get('displayName', 'Unknown')} - Live Stats",
                        color=discord.Color.blue()
                    )

                    # Add team info
                    team_name = competitor.get('team', {}).get('displayName', 'Unknown')
                    embed.add_field(name="Team", value=team_name, inline=True)

                    # Add position
                    if athlete.get('position'):
                        embed.add_field(name="Position", value=athlete.get('position', {}).get('abbreviation', 'N/A'),
                                        inline=True)

                    # Add jersey number
                    if athlete.get('jersey'):
                        embed.add_field(name="Jersey", value=f"#{athlete.get('jersey')}", inline=True)

                    # Add game stats
                    stats = athlete.get('statistics', [])
                    if stats:
                        stats_text = []
                        for stat in stats:
                            stat_name = stat.get('displayName', stat.get('name', 'Unknown'))
                            stat_value = stat.get('displayValue', 'N/A')
                            stats_text.append(f"**{stat_name}:** {stat_value}")

                        if stats_text:
                            embed.add_field(name="Game Stats", value="\n".join(stats_text[:8]), inline=False)
                    else:
                        embed.add_field(name="Game Stats", value="No stats available yet", inline=False)

                    # Add game context
                    opponent = next((c for c in competitors if c != competitor), {})
                    opponent_name = opponent.get('team', {}).get('displayName', 'Unknown')
                    status = game.get('status', {}).get('type', {}).get('detail', 'Scheduled')

                    embed.add_field(name="Game", value=f"vs {opponent_name}", inline=True)
                    embed.add_field(name="Status", value=status, inline=True)

                    await ctx.send(embed=embed)
                    break

            if player_found:
                break

        if player_found:
            break

    if not player_found:
        await ctx.send(
            f"❌ Player '{player_name}' not found in today's {sport.upper()} games. Try using the `;player` command for general player info.")


@bot.command()
async def updates(ctx, sport: str, *, team_or_player: str = None):
    """Subscribe to live updates for a sport, team, or player"""
    sport = sport.lower()
    if sport not in SPORT_APIS:
        await ctx.send(f"❌ Invalid sport. Available: {', '.join(SPORT_APIS.keys())}")
        return

    if team_or_player:
        await ctx.send(
            f"✅ Now tracking live updates for **{team_or_player}** in {sport.upper()}!\n💡 Tip: Use `;livescore {sport} {team_or_player}` for manual updates or `;track {sport}` for automatic game updates.")
    else:
        # Add to tracking
        tracker.tracked_sports.add(sport)
        await ctx.send(
            f"✅ Now tracking all {sport.upper()} games! Updates will be posted automatically.\n💡 Use `;livescore {sport}` to see current live scores.")

        if not update_sports.is_running():
            update_sports.start()


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user.name} (ID: {bot.user.id})")
    print(f"🌐 Connected to {len(bot.guilds)} server(s)")
    print(f"🔗 Health endpoint available at port {os.getenv('PORT', 10000)}")
    print("=" * 50)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument: {error.param.name}")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    else:
        await ctx.send(f"❌ An error occurred: {str(error)}")
        print(f"Error: {error}")


async def shutdown():
    """Clean shutdown"""
    await tracker.close()
    await bot.close()


if __name__ == "__main__":
    # Start Flask in a separate thread for health checks
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🌐 Flask server started on port {os.getenv('PORT', 10000)}")

    # Start the bot
    try:
        bot.run(token, log_handler=handler, log_level=logging.DEBUG)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        asyncio.run(shutdown())