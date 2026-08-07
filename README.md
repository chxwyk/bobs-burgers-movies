# Oxy Movie Ticket Bot

A standalone Discord movie-order bot based on the proven Bob's Burger ticket flow,
rebuilt strictly for movie tickets.

## What it does

- Original purple movie storefront with **Order Movie** and **How It Works** buttons
- Movie form for movie/showtime, ZIP code, seats, AMC snacks, and final checkout total
- Enforces a **$40 minimum** and **$250 maximum** final checkout total
- Calculates exactly **50% off** using integer cents instead of floating-point money
- Creates one private ticket visible only to the customer, Movie Staff, and the bot
- Automatically pings the configured Movie Staff role when a new ticket opens
- Staff claim, invoice, payment confirmation, QR-ticket delivery, completion, and closure
- Each Movie Staff member can configure their own payment methods
- Red **Close Ticket** button plus `/close`
- Saves an HTML transcript before deleting a closed ticket
- Persistent open/closed status and buttons across Railway restarts
- `/store open` pings the customer notification role only on a closed-to-open change
- `/store close` closes new orders silently and leaves current tickets available
- Bundled **Bob's Burgers Direct Movies** PFP automatically applies on first startup
- SQLite database and Railway volume support

This bot manages the customer request and staff workflow. It does not purchase tickets,
access theater accounts, bypass checkout systems, or automatically verify payments.

## Customer flow

1. Select **Order Movie**.
2. Enter the movie/showtime, ZIP code, seat count, AMC snacks or `None`, and the
   final checkout total.
3. The bot creates a private movie ticket and pings Movie Staff.
4. Upload a full screenshot of the theater checkout page showing the movie,
   theater, date, showtime, seats, items, and final total.
5. Movie Staff verifies the screenshot and sends the exact 50%-off invoice.
6. Choose a payment method, pay, select **I've Paid**, and upload proof.
7. Movie Staff posts the QR ticket codes and any AMC snack pickup information.

## Staff commands

```text
/claim
/pay
/pay final_total:86.42
/paid
/tickets_sent details:QR codes attached. AMC snacks are under the customer's name.
/complete
/close reason:Tickets delivered successfully
/order_info
```

Payment setup is separate for each Movie Staff member:

```text
/payments set method:Cash App instructions:Send $AMOUNT to $YourTag
/payments set method:Apple Pay instructions:Send $AMOUNT to (555) 555-5555
/payments list
/payments remove method:Cash App
```

Only administrators and members with the configured Movie Staff role can use
the staff order, payment, and storefront commands.

## Store availability

```text
/store close
/store open
```

- Closing disables **Order Movie** and blocks new ticket creation without pinging.
- Opening enables ordering and pings the configured customer notification role once.
- Running `/store open` again while already open does not send another ping.
- Existing tickets stay open when the storefront closes.

## 1. Create the separate Discord bot

1. Open the Discord Developer Portal: <https://discord.com/developers/applications>
2. Select **New Application** and name it `Oxy Movie Tickets`.
3. Open **Bot**, select **Reset Token**, and copy the token privately.
4. Enable **Message Content Intent** so ticket conversations appear in transcripts.
5. Open **OAuth2 → URL Generator**.
6. Select `bot` and `applications.commands`.
7. Give the bot these permissions:
   - View Channels
   - Send Messages
   - Manage Channels
   - Embed Links
   - Attach Files
   - Read Message History
   - Use Application Commands
   - Mention @everyone, @here, and All Roles
8. Open the generated invite URL and add the bot to your server.

Keep the token private. Never upload `.env` or paste the token in Discord or GitHub.

## 2. Create the Discord roles and channels

Create these before running setup:

- A public movie storefront channel, such as `#movie-orders`
- A private movie ticket category
- A private transcript/log channel
- The **Movie Staff** role you already created
- A customer notification role, such as **Movie Pings**

The bot already contains these server role defaults:

```text
Direct Movies Staff: 1535138119664402443
Customer notifications: 1515866262306033737
```

The Movie Staff role is automatically granted access to each new ticket and is pinged
when a customer submits the order form. The Movie Pings role is only pinged when staff
changes the storefront from closed to open.

## 3. Configure the bot in Discord

After Railway shows the bot as online, run:

```text
/setup
```

Select:

- `panel_channel`: public movie storefront channel
- `ticket_category`: private movie ticket category
- `transcript_channel`: private transcript channel
- `movie_staff_role`: optional override; defaults to **Direct Movies Staff**
- `notification_role`: optional override; defaults to your customer role
- `brand_name`: optional; defaults to `Oxy Movies • 50% Off`
- `banner_url`: optional movie storefront artwork URL

The bot posts the complete storefront automatically. Use `/panel` later to refresh it.

## 4. Deploy on Railway

1. Create a new private GitHub repository for this movie bot.
2. Upload every file and folder from this project into the repository root.
3. Create a new Railway project from that GitHub repository.
4. Add these Railway variables:

```text
DISCORD_TOKEN=your_new_movie_bot_token
DEV_GUILD_ID=your_discord_server_id
MOVIE_STAFF_ROLE_ID=1535138119664402443
CUSTOMER_NOTIFICATION_ROLE_ID=1515866262306033737
DATABASE_PATH=/app/data/movie_orders.db
TRANSCRIPT_DIR=/app/data/transcripts
LOG_LEVEL=INFO
```

5. Add a Railway persistent volume mounted at:

```text
/app/data
```

6. Deploy and wait for the logs to show `Ready as`.

On the first successful startup, the bot automatically uploads the bundled
`movie_bot/assets/bobs-burgers-direct-movies-pfp.png` as its Discord profile picture.
The successful avatar version is stored in the persistent database so reconnects and
ordinary restarts do not repeatedly update the profile.

The persistent volume keeps settings, orders, payment methods, storefront status,
and transcript files through redeploys.

## Local tests

Python 3.12 is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check movie_bot tests
ruff format --check movie_bot tests
```

On Windows, activate the environment with `.venv\Scripts\activate`.
