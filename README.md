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
- `/complete` and `/done` record the owner's 12% share under the claiming Movie Staff member
- `/complete` closes immediately; `/done` gives the customer one hour to save their
  ticket information before the bot archives and deletes the channel—even after a Railway restart
- The 12% uses the customer's 50%-off price shown on the right, never the original
  checkout total shown on the left
- Duplicate-safe earnings ledger with repaired historical totals, customer revenue,
  owner profit/share, and per-staff breakdown
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
8. Movie Staff runs `/complete` for an immediate close or `/done` for a one-hour
   customer access window. Both commands immediately record the owner's 12% share
   as owed by the claimant.
9. The 12% is calculated from the customer's 50%-off price on the right side of
   the order summary. The bot then saves the final transcript and closes automatically.

## Staff commands

```text
/claim
/pay
/pay final_total:86.42
/paid
/tickets_sent details:QR codes attached. AMC snacks are under the customer's name.
/complete
/done
/close reason:Tickets delivered successfully
/order_info
```

Both completion commands require no amount and no manual calculation. The bot uses
the checkout total submitted in the order form, or the corrected total saved with
`/pay final_total:`, calculates the customer's 50%-off price, then records 12% of
that right-side customer price. For example, a `$120.00` checkout produces a
`$60.00` customer price and a `$7.20` owner share owed by the claiming Movie Staff
member. Each order is counted once and immediately appears in `/earnings`.
`/complete` archives immediately; `/done` keeps the channel available for one hour.
Scheduled closures survive Railway restarts. Payment must be confirmed with `/paid`
first, and customers never see the owner-share calculation.

For an older ticket that says it is not recognized, recover it inside that channel:

```text
/complete legacy_total:42
```

The bot reads the customer ID from the older channel topic, reconnects the channel,
records `$2.52` at 12% of the `$21.00` customer price, and closes immediately. If
the topic does not contain a customer ID, use
`/complete legacy_total:42 customer:@Customer`. Do not enter `legacy_total` on
normal recognized tickets.

Use `/close` for cancelled, duplicate, or unfinished tickets. It saves the
transcript without recording earnings.

Administrators can view the running total with:

```text
/earnings
```

On the first startup after this update, the bot automatically recalculates every
older ledger entry from the right-side customer price. `/earnings` then shows the
corrected lifetime customer revenue, owner profit/share, and claimant balances.

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
OWNER_SHARE_PERCENT=12
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
owner-share totals, and transcript files through redeploys. `OWNER_SHARE_PERCENT`
defaults to `12`, so the variable is optional unless you want to change the rate.

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
