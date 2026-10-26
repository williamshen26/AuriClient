# Hey Auri Client for Home Assistant

Hey Auri Client connects Home Assistant to [Auri](https://hey-auri.com), a cloud voice assistant. You talk to Auri through Home Assistant's Assist, by voice or text. Auri works out what you want in the cloud, and the actions on your devices run locally in your Home Assistant.

The integration adds three things to Home Assistant:

- **A conversation agent** for Assist.
- **Hey Auri Cloud STT**: streaming speech-to-text.
- **Hey Auri Cloud TTS**: streaming text-to-speech with two voices.

## Features

- **Device control**: lights (on/off, brightness, color), covers and blinds (position, tilt), climate (temperature, humidity, fan and HVAC mode) and media players (power, playback, volume, mute, shuffle, source and app switching). TVs can be woken with Wake-on-LAN.
- **Music** via [Music Assistant](https://music-assistant.io): search and play, and move what's playing to another speaker.
- **Everyday skills**: weather forecasts, calendar events, shopping list, timers, sticky notes, and creating and editing automations.
- **Long-term memory** (optional, owner mode): Auri remembers facts, preferences and past conversations in your own PostgreSQL database, and can keep a readable copy in an Obsidian vault. See [Memory](#memory).
- **Places and rides**: search for places, get directions, and request Uber ride links. Links arrive by text message or on the [session QR page](#session-qr-code).
- **Guest mode** for short-term rentals: answers guests' questions from the details you enter (check-in and check-out, Wi-Fi, parking, house rules and more) and can notify you.
- **42 languages**: say "speak Spanish to me" and Auri switches both listening and speaking.
- **Personalities**: Auri, Tovi, Glen or Brigg, chosen on your dashboard at hey-auri.com.
- **Quick confirmations**: simple device commands get an immediate spoken "OK" instead of waiting for a full reply.

## Requirements

- An Auri account from [hey-auri.com](https://hey-auri.com). Your dashboard there shows the **Client ID** and **Client secret** you need.
- Home Assistant with [HACS](https://hacs.xyz/docs/use/) installed.
- Optional:
  - A PostgreSQL server with pgvector for [memory](#memory).
  - [Music Assistant](https://music-assistant.io) for music.
  - A voice satellite, such as Home Assistant Voice Preview Edition, for hands-free use.
  - The [Wake on LAN](https://www.home-assistant.io/integrations/wake_on_lan/) integration for turning on TVs.

## Installation

1. **Create your Auri account** at [hey-auri.com](https://hey-auri.com). After you finish checkout (or redeem an invitation code), your dashboard shows your Client ID and Client secret. You can come back to the dashboard to see them at any time.
2. **Install HACS** if you haven't already ([guide](https://hacs.xyz/docs/use/)).
3. **Download Hey Auri Client**: in HACS, search for **Hey Auri Client** and select **Download**. If it doesn't appear, first add `https://github.com/williamshen26/AuriClient` as a custom repository with the type **Integration**.
4. **Restart Home Assistant.**
5. **Add the integration**: go to **Settings → Devices & services → Add integration**, search for **Hey Auri Client**, and enter:
   - **Client ID**: from your dashboard.
   - **Shared secret**: the **Client secret** from your dashboard.

> Setup only checks that Home Assistant can reach the Auri cloud. Wrong credentials show up the first time you talk to Auri (see [Troubleshooting](#troubleshooting)).

## Set up a voice assistant

1. Go to **Settings → Voice assistants** and add a new assistant, or edit an existing one.
2. Set **Conversation agent** to **Hey Auri Client** (or the name you gave the integration).
3. Turn off **Prefer handling commands locally** so that every request goes to Auri.
4. Set **Speech-to-text** to **Hey Auri Cloud STT** and **Text-to-speech** to **Hey Auri Cloud TTS**. Both are recommended, because Auri's language switching works through them.
5. Choose a voice: **Auri** (female) or **Glen** (male). The voice choice appears when the assistant's language is English. Other languages use the Auri voice.
6. Save.

Auri can only see and control entities that are exposed to Assist. Choose them under **Settings → Voice assistants → Expose**.

## Owner and guest mode

Choose the mode on your dashboard at hey-auri.com:

- **Owner**: everything described above, including automations and [memory](#memory).
- **Guest**: for rental guests.
  - Automation editing is turned off, and Auri never uses what it remembers about your household. Guests get memory of their own only if you turn on [guest memory](#guest-memory); otherwise nothing they say is saved.
  - Auri answers questions using the [Guest options](#options) and can send you a notification.
  - If you've filled in **Local recommendations**, Auri suggests those places before searching.

### Bookings from your calendar

Instead of typing in each booking, give Auri your listing's calendar link, and it fills in the booking ID, check-in and check-out by itself.

1. **Copy the link.** On Airbnb, go to **Calendar → Availability → Connect calendars → Export calendar** and copy the link. Any other iCal link works too (VRBO, Booking.com or a property-management tool).
2. **Paste it** into **Booking calendar link** under **Configure → Booking calendar**.
3. **Set your usual times.** On the same page, check **Usual check-in time** and **Usual check-out time** (15:00 and 11:00 by default).

From then on:

- **Checks:** Auri reads the calendar every 30 minutes, so a same-day booking shows up in time.
- **Changeover:** a booking stays in the Guest options until an hour after its check-out, so guests who leave late still have their details and guest memory. Then the next booking takes over, even when the next guests arrive the same afternoon.
- **What the calendar fills in:** Airbnb's calendar has dates and a reservation code, but no guest name, number of guests, phone number or times.
  - Times come from your usual check-in and check-out times, for every booking.
  - When a new booking takes over, the guest name, count, phone number and notes are cleared, because they belonged to the last guests. Fill them in if you like. Calendars that include names, such as some property-management tools, fill them in for you.
- **In the Guest options:** while there's a link, the Booking ID, Check-in and Check-out fields are hidden, because the calendar decides them. The form shows the booking the calendar filled in instead. The guest name, count, phone number and notes stay yours, and within the same booking they're kept. An extended stay moves check-out without starting guest memory again.
- **Changing the link:** a new link starts afresh. The booking the last link filled in is replaced right away by the new calendar's, or cleared if the new calendar has nothing coming. A booking you typed in yourself that's under way when you first add a link stays until it ends.
- **Cancellations:** a booking cancelled before check-in is replaced by the next one. A stay that has started is never cut short, even though Airbnb's calendar stops listing it once it begins.
- **Ignored:** dates you've blocked on Airbnb.
- **Problems:** if the calendar can't be read (for example after you reset the link on Airbnb), the Guest options keep the last booking, and after 6 hours Home Assistant shows a notification.

The **Auri Guest Stay** sensor (`sensor.auri_guest_stay`) shows `none`, `upcoming`, `staying` or `checked_out`, with the booking, the next booking, and when the calendar was last read. It also follows bookings you type in, so you can build automations on it, such as heating on before check-in or eco mode after check-out.

The link shows when your home is empty, so keep it private. Auri never writes it to its logs, and doesn't keep the phone digits Airbnb includes in the calendar.

## Options

Go to **Settings → Devices & services → Hey Auri Client → Configure**.

**General**

| Option | Description |
|---|---|
| Request timeout (seconds) | How long to wait for the Auri cloud (1–300, default 30). |
| STT default language | The language Auri listens and replies in. Auri changes it for you when you ask to switch languages. |
| TTS stream endpoint | Advanced. Leave it at the default. |

**Guest** (used in guest mode)

| Option | Description |
|---|---|
| Booking ID, Guest name, Check-in, Check-out, Guest count, Notes | Details of the current booking. Booking ID, Check-in and Check-out are hidden while there's a booking calendar link. |
| Guest phone number | US and Canada numbers only (`+16045551234` or `6045551234`). Used to text ride links to the guest. |
| Check-in, Check-out, Wi-Fi, Parking, Trash disposal and Host contact instructions; House rules | What Auri tells guests when they ask. |
| Property knowledge | Where things are and how to use them, for example where the spare towels are. |
| Local recommendations | Your favorite places nearby. Auri suggests these before searching. |
| Let guests use memory | Off by default. Gives guests a memory of their own for the stay. See [Guest memory](#guest-memory). |
| Reset guest memory | Tick and save to erase guest memory and start it again from the guest details. |

**Booking calendar** (optional)

| Option | Description |
|---|---|
| Booking calendar link | Your listing's calendar link. With it, the booking ID, check-in and check-out in the Guest options fill in by themselves. Clear it to enter bookings yourself. See [Bookings from your calendar](#bookings-from-your-calendar). |
| Usual check-in time, Usual check-out time | Used for every booking, because Airbnb's calendar gives dates but no times (15:00 and 11:00 by default). |

**Memory** (optional)

Connects Auri's long-term memory to your PostgreSQL database. See [Memory](#memory) for the options.

**Metrics** (optional)

Sends the response time of each request to the Auri cloud to your own InfluxDB 1.x server. Fill in the host, port (default 8086), database, username, password, measurement name (default `saas_request_latency`), and whether to use SSL.

## Memory

With memory set up, Auri remembers what your household tells it in owner mode: facts ("Mia's piano lesson moved to Thursday"), preferences, decisions and plans, a short summary of each day, and the past week's conversations. It uses them to answer ("When is Mia's piano lesson?", "What did we decide about the trip?") and to serve you better without being told again.

Everything is stored in a PostgreSQL database you run. The Auri cloud doesn't keep any of it (see [What is sent to the Auri cloud](#what-is-sent-to-the-auri-cloud)). Without a database there's no memory: Auri works as before and says it can't remember things.

### What you need

PostgreSQL 13 or newer with the [pgvector](https://github.com/pgvector/pgvector) extension, version 0.5 or newer (0.8 or newer recommended).

- **Home Assistant OS**: install the **TimescaleDB** app (add-on), which includes pgvector. Its host name is `77b2833f-timescaledb`.
- **Home Assistant Container**: run the `pgvector/pgvector:pg17` image.
- **Home Assistant Core**: Home Assistant OS and Container include the PostgreSQL driver, but a Core install needs it in Home Assistant's virtual environment: `pip install psycopg2-binary`.

Create a database and a user for Auri. Connect as an administrator and run (in `psql`; `\c auri` switches to the new database):

```sql
CREATE ROLE auri LOGIN PASSWORD 'choose-a-password';
CREATE DATABASE auri OWNER auri;
\c auri
CREATE EXTENSION IF NOT EXISTS vector;
```

Auri creates its own tables, in a schema called `auri_memory`. Use one database for each Home Assistant.

### Set it up

Go to **Settings → Devices & services → Hey Auri Client → Configure → Memory**, turn on **Enable long-term memory**, and fill in:

| Option | Description |
|---|---|
| PostgreSQL host, port, database name, username, password | Where the database is (port 5432 by default). After saving, leave the password empty to keep the saved one. |
| SSL mode | `prefer` by default. Use `require` or stricter when the database is on another machine. |
| Obsidian vault folder | Optional. See [Obsidian copy](#obsidian-copy). |
| Include private memories in the vault copy | Off by default. |
| A day ends at (hour) | 4 by default: talk after midnight counts toward the day before. |
| Keep the conversation archive for (days) | 365 by default; 0 keeps it forever. |
| Share memories within the household | Off by default. See [Who is speaking](#who-is-speaking). |

Saving tests the connection without changing anything, and tells you what's wrong if it can't be used: wrong password, no pgvector, a user without the rights to enable pgvector or create the schema, and so on. The **Auri Memory** sensor (`sensor.auri_memory`) then shows `enabled`.

The password is stored in Home Assistant's configuration, like the passwords of other integrations.

### How Auri learns

- **During a conversation**, Auri gets a short profile of important facts about the speaker and the household, plus notes that match what was said. It can also look further back. These lookups happen in Home Assistant, in a fraction of a second.
- **When you ask** ("remember that…", "Mia doesn't eat pork anymore"), Auri saves it right away. A correction ("Mia's piano lesson moved to Thursday") replaces the old memory right away too; the old version is kept as history.
- **Every night**, about 15 to 25 minutes after the day ends, the day's conversations are sent to the Auri cloud to be summarized. Auri keeps:
  - a summary of the day;
  - the day's conversations, for a week;
  - the facts and preferences it learned.

  A new fact either adds to what Auri knows, replaces what changed, or confirms it. The old version is kept as history. If Home Assistant was off, missed days (up to a week back) are processed after it starts. A day that fails is tried again on later runs, up to three attempts in all.

Appointments with a date or time go in your calendar, after Auri checks with you, even when you say "remember…". If you mention one in passing, Auri offers once to add it. Without a calendar, or if you'd rather not, Auri keeps them in memory. Things that repeat ("piano every Thursday") are kept in memory as routines, because Auri can't add repeating events to the calendar, and they don't expire. When you ask what's coming up, Auri checks both the calendar and memory.

Appointments and plans in memory are saved with their actual date ("this Thursday" becomes Thursday 2026-10-15), so they still point at the right day weeks later. Once the date has passed, Auri treats them as past, and 14 days after it stops using them.

Auri never stores passwords, codes, or card or account numbers, and removes them from conversations before they're saved. Short device commands ("turn off the lights") and requests from automations aren't learned from.

### Who is speaking

When you use Assist while signed in to Home Assistant, Auri knows who you are. Voice satellites can't tell who is talking, so from a satellite:

- facts about the household or a named person ("the bins go out on Tuesday", "Mia's piano lesson moved to Thursday") are saved as they are;
- for something personal ("remember I like oat milk"), Auri asks "Who should I remember this for?", once per conversation, and remembers your answer for the rest of it.

People and pets don't have to be in Home Assistant to be remembered: "remember Lillian likes candy" is saved about Lillian, and Auri learns your pets from what you say. Say who someone is the first time ("my girlfriend Lillian likes candy", "our dog Biscuit is allergic to chicken"), so Auri can also answer questions that don't use their name. A pet added to Home Assistant as a person (for example to track its collar) is treated as a person.

When you're signed in, Auri takes you to be the person your login is linked to in Home Assistant. If someone else uses a login (you test with the admin account, say), or the household shares one, like a wall tablet, set that in the integration's options under **Who uses each login**. A shared login is treated like a voice satellite. Mark a login a service uses, such as Zigbee2MQTT's MQTT login, as **Not a person**, so its person isn't listed as a household member. You don't need to delete it in Home Assistant: deleting a person there also deletes its login.

Memories about health, money, contact details and birthdays are marked **private**: Auri uses them only for the person they're about.

On a voice satellite, Auri doesn't know who is talking, so it doesn't use memories about any one person. If nobody in your household minds, turn on **Share memories within the household** in the Memory options. Auri then uses the important memories about everyone, private ones included, whoever is speaking. For example, "can I have milk?" from a satellite gets a reminder that Mia is lactose intolerant, in case it's for Mia. Anyone near a satellite could hear them, visitors too.

### Forgetting

Say "forget that…" and Auri stops using the memory right away, along with saved conversations about it. Daily summaries that mention it are rewritten the next night, in the Obsidian copy too. Forgotten memories stay in the database, marked as forgotten, but they're never used again, and Auri doesn't learn them again unless someone says them again on a later day.

### Guest memory

When you rent your home out, guests can have a memory of their own. Turn on **Let guests use memory** in the Guest options (Memory has to be set up). In guest mode, Auri then remembers what guests ask it to, learns from their conversations each night, and uses what it knows during the stay, as it does for you. It's kept in its own schema, `auri_guest_memory`, in the same database:

- Guests never get your memories, and you never get theirs: each mode only uses its own.
- It starts with the guest details from the Guest options: the guest's name, the number of guests, check-in and check-out, and your notes (a line that looks like a password or code is left out). Property information such as Wi-Fi or house rules isn't copied; Auri reads it from the Guest options when asked.
- It's erased completely an hour after check-out, so guests who leave a little late can still use it. From then until you save the next booking, it's closed: Auri remembers nothing for guests and keeps nothing they say. If the database can't be reached at that moment, the erase happens at the next check, within 15 minutes of it coming back.
- It starts again from the guest details when a new booking ID is saved (or a new check-in, if you don't use booking IDs). With a [booking calendar link](#bookings-from-your-calendar), that happens by itself an hour after each check-out. If you type bookings in, enter each one after the previous guests have left. To start again yourself, tick **Reset guest memory** and save; after a stay has ended this erases it but doesn't reopen it. Turning guest memory off erases it too.
- Guests don't sign in. When the Guest options name a single guest, what they say about themselves is saved for them; with more, Auri asks who it's for, once per conversation.
- Plans guests ask Auri to remember ("we have a dinner reservation Friday at 7") stay in guest memory and aren't added to your calendar, unless a guest asks for a calendar event.
- It's never copied to Obsidian.

Guest memory is stored in your database until it's erased, so you could read it. Let guests know the home has an assistant that can remember things during their stay. Home Assistant backups made during a stay include it.

### Obsidian copy

Set **Obsidian vault folder** to your vault's folder, for example `/share/MyVault`, and every night Auri writes into it:

- `Auri/Daily/YYYY-MM-DD.md` with the summary of each day;
- `Auri/Memory.md` with everything it currently remembers, grouped by person and subject.

It's a copy for reading: the database stays the source, and edits made in the vault aren't read back. Home Assistant must be allowed to write to the folder, so add it to `configuration.yaml` and restart:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /share/MyVault
```

A folder inside `www`, which Home Assistant serves without a login, is refused.

### Testing what Auri remembers

To check how well memory works, create `hey_auri_memory_exam.yaml` in your configuration folder with questions and the answers you expect:

```yaml
- q: What day is Mia's piano lesson?
  expected: Thursday.
- q: Where are we going on vacation this summer?
  expected: Portugal, in July.
```

Then call `hey_auri_client.run_memory_exam`. Each question (up to 50) is asked in a new conversation, and the answers are graded. The result has a score from 0 to 1 and the grade for each question. Exam conversations aren't saved to memory. The questions run like real requests, so ask questions, not commands.

### Old saved preferences

Earlier versions saved preferences in `www/user_data`, a folder Home Assistant serves without a login. This version moves those files to `.storage/hey_auri_client_legacy_user_data` on startup and shows a notification. Once memory is set up, each file is imported as preferences of the person it belongs to. A file whose name doesn't match exactly one Home Assistant user is left out, and the notification lists it.

## Session QR code

The session QR code gives guests (or you) a page on their phone that lists the links Auri shares, such as Uber ride links and map locations.

- The **Refresh Session** button (`button.refresh_session`) starts a new session with a new page. Press it between guests. Links shared after a refresh go to the new page, and every link expires after 7 days.
- The **Session Link** sensor (`sensor.session_link`) holds the current page address. Until you press Refresh Session for the first time, it shows `https://hey-auri.com`.

To show the code on a dashboard, add the bundled card. It loads automatically and appears in the card picker as **Auri Session QR Code**:

```yaml
type: custom:auri-session-qr-card
entity: sensor.session_link
placeholder: Waiting for a session…  # optional
```

## Timers and sticky notes

**Timers.** Each running timer appears as a sensor (`sensor.auri_timer_<id>`) that shows the seconds remaining. When a timer finishes, the integration fires the `hey_auri_client.auri_timer_finished` event and removes the sensor. The integration does not ring anything by itself.

To ring the satellite that started the timer, add an automation like the one below. It uses the satellite's **Timer Ringing** switch, which the event includes when the satellite has one (Voice Preview Edition does):

```yaml
triggers:
  - trigger: event
    event_type: hey_auri_client.auri_timer_finished
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.satellite_timer_ring is not none }}"
actions:
  - action: switch.turn_on
    target:
      entity_id: "{{ trigger.event.data.satellite_timer_ring }}"
```

**Sticky notes.** Each note is a sensor (`sensor.auri_sticky_note_<id>`) whose content is Markdown, so you can show it in a Markdown card. Notes are kept across restarts.

## Services

| Service | What it does |
|---|---|
| `hey_auri_client.start_auri_timer` | Starts a timer. Takes `duration` in seconds and an optional `satellite_speaker`. |
| `hey_auri_client.get_auri_timers` | Returns the running timers. |
| `hey_auri_client.create_auri_sticky_note` | Creates a note. Takes `markdown`, plus optional `title` and `note_id`. Fails if the `note_id` already exists. |
| `hey_auri_client.delete_auri_sticky_note` | Deletes the note with the given `note_id`. |
| `hey_auri_client.get_auri_sticky_notes` | Returns all notes. |
| `hey_auri_client.assign_mac` | Adds MAC addresses to a media player for Wake-on-LAN. Use it when a device has more than one network interface and Home Assistant only knows one of them. |
| `hey_auri_client.sync_bookings` | Reads the booking calendar now, rather than waiting for the next 30-minute check, and returns what it found: the number of bookings, the next booking, and whether the Guest options changed. |
| `hey_auri_client.run_memory_job` | Runs the nightly memory processing now, for your memory and then the guests' (when guest memory is on). Without `date`, processes the days that were missed (`days_back`, default 7). With a `date`, processes that day, which must be over; add `force: true` to process a day again. Returns what was learned. |
| `hey_auri_client.run_memory_exam` | Asks the questions in `hey_auri_memory_exam.yaml` and returns the score (see [Testing what Auri remembers](#testing-what-auri-remembers)). |

## Events

| Event | Data |
|---|---|
| `hey_auri_client.conversation_finished` | `conversation_id`, `client_id` (the config entry ID), `user_input`, `response` |
| `hey_auri_client.auri_timer_finished` | `timer_id`, `entity_id`, `duration`, `satellite_speaker`, `satellite_timer_ring` |
| `hey_auri_client.memory_run_finished` | `day`, `scope` (`owner` or `guest`), `status` (`succeeded`, `failed` or `skipped`), `reason`, and when it succeeded `digest`, `exchanges`, `facts_added`, `facts_updated` and `facts_confirmed` |

The **Auri Memory** sensor (`sensor.auri_memory`) shows whether memory works: `disabled` (not set up), `starting`, `enabled`, `unavailable` (the database can't be reached; Auri keeps working without memory and catches up later) or `error` (a setup problem; see its `error` attribute). Its attributes also show the last nightly run, guest memory's state (`guest_memory`: `on`, `off`, or `closed` after a stay has ended), and when it was last reset or erased.

## What is sent to the Auri cloud

- **With each request**:
  - your message;
  - the exposed entities' IDs, names, aliases, states, areas and selected attributes;
  - your home's location (latitude and longitude, time zone, country);
  - the current time, the Home Assistant user's name, the language, and which device and area you're speaking from.
- **For voice**: your audio, to transcribe it, and Auri's reply text, to turn it into speech.
- **When Auri needs it**: the results of the tools it uses, such as your shopping list, your calendar events or the guest instructions you configured.
- **With memory set up**, in owner mode (and in guest mode with guest memory on, for the guests' memory):
  - with each request: the speaker's name, the names of the household's people and pets, and the memory notes and profile Home Assistant picked for that message;
  - the text of memories that need a search key (an embedding), such as preferences imported from an earlier version, so the cloud can compute it;
  - every night: the day's conversations (with passwords, card numbers and similar removed), the names of the people, pets and subjects Auri knows, and, for each new fact, the few stored memories most like it, so the cloud can summarize the day and tell what's new. The cloud returns the summary and the facts and keeps none of it.

Guest details, timers and notes are stored in Home Assistant, and memories in your own PostgreSQL database. The cloud uses conversation history for about 5 minutes, so that follow-up questions work. After that the history expires and is deleted automatically, usually within a few days. If memory debugging is turned on for your account, the memory notes and profile sent with each message are kept in that history too, and expire with it. See the [Privacy Policy](https://hey-auri.com/privacy.html).

## Troubleshooting

| What you see or hear | What to do |
|---|---|
| "There is an issue with your account, please log in to resolve it" | The voice service rejected your credentials, or your subscription isn't active. Log in at [hey-auri.com](https://hey-auri.com) to check your subscription, and make sure the Client ID and Shared secret match your dashboard. |
| "There is a limit reached, please try again in a bit." | You've reached a usage limit. Try again later. |
| "Something went wrong: …" | This usually means wrong credentials or a network problem. Check the Client ID and Shared secret, then check the logs. |
| Auri doesn't know about a device | Expose the entity under **Settings → Voice assistants → Expose**. |
| Auri asks you to set up Music Assistant | Music playback needs [Music Assistant](https://music-assistant.io). |
| Auri listens or replies in the wrong language | Ask it to switch (for example, "speak English"), or change **STT default language** in the General options. |
| The QR code opens the Auri home page | Press **Refresh Session**. |
| Auri says memory isn't set up | Set it up under **Configure → Memory** (see [Memory](#memory)). In guest mode, memory is off unless **Let guests use memory** is on in the Guest options (see [Guest memory](#guest-memory)). |
| Saving the Memory options fails | The message says what to fix. Most often pgvector isn't enabled in the database: run `CREATE EXTENSION vector;` in it as an administrator. |
| `sensor.auri_memory` is `unavailable` | Home Assistant can't reach the database. Auri keeps working without memory, holds on to the latest conversation (up to 1,000 messages), and saves it when the database is back. |
| `sensor.auri_memory` is `error` | Its `error` attribute names the problem, for example `pgvector_missing`, `insufficient_privilege` or `schema_too_new` (the database was set up by a newer version of this integration: update it). |
| A booking didn't fill in from the calendar | Look at the `error` and `last_sync` attributes of `sensor.auri_guest_stay`, or run `hey_auri_client.sync_bookings`. If you reset the link on Airbnb, paste the new one under **Configure → Booking calendar**. |
| Auri didn't learn something from yesterday | Check the `last_nightly_run` attribute of `sensor.auri_memory`, or run `hey_auri_client.run_memory_job` and look at what it returns. |

To turn on detailed logs, add this to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.hey_auri_client: debug
```

## Support

- Website: [hey-auri.com](https://hey-auri.com)
- Issues: [github.com/williamshen26/AuriClient/issues](https://github.com/williamshen26/AuriClient/issues)

## License

See the repository license file, if available.
