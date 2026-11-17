# EmergencyAlertArr

**A software ENDEC for Dispatcharr.** EmergencyAlertArr watches the National Weather Service and
FEMA IPAWS for alerts in your area. When one is issued, it interrupts whatever channel you're
watching in [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) with a broadcast-style EAS
screen, the real SAME header and attention tones, and a spoken readout. Then it puts the channel
back as if nothing happened.

If you've ever wanted your IPTV channels to behave like real cable during a tornado warning, that's
the idea.

**Current version: 0.9.10**

---

## Contents

- [Features](#features)
- [Screen styles](#screen-styles) (all 22, with previews)
- [Requirements](#requirements)
- [Install](#install)
- [Settings](#settings)
- [Actions](#actions)
- [How the channel switch works](#how-the-channel-switch-works)
- [Version history](#version-history)
- [Responsible use](#responsible-use)

---

## Features

### Alerts
- Polls **NWS** (`api.weather.gov`) and/or **FEMA IPAWS-OPEN**, filtered to your area by NWS
  zone/county codes and 6-digit SAME/FIPS codes.
- Plays the **IPAWS alert's own recorded audio** when the alert carries one; otherwise reads the
  alert text aloud.
- **Proper EAN handling:** a national alert interrupts anything, holds until it's over, and anyone
  tuning in meanwhile gets it. An EAT ends it.
- **National test mode:** receive every alert in the country, to try it out.
- Alerts play one at a time with spacing between them, like a real ENDEC. Old alerts and duplicates
  (the same alert from NWS and IPAWS) aren't played twice.

### Only where someone is watching
- Alerts go out only on armed channels that **someone is actually watching**. Arming every channel
  is fine.
- **5-second join window:** flip to an armed channel right as an alert goes out and you get it too.
- An alert nobody saw isn't saved up for whoever tunes in later.
- Leave a channel mid-alert and it goes back to normal on its own.

### Encoder realism
- **22 screen styles** from DASDEC, Trilithic EASyPLUS / EASyCAP, Gorman-Redlich, VDS Sub-Alert,
  generic set-top boxes and Scientific-Atlanta SARA firmware.
- **Encoder-accurate SAME tones.** Each style uses its encoder's documented byte framing, burst
  gaps and audio character. That covers DASDEC, Sage 3644 / 1822, Trilithic, EASyCAP, TFT,
  Gorman-Redlich and NWS weather radio.
- **Relaying:** keeps the originator's issue time and valid period, with your station ID in the
  outgoing header. The header lists the real county codes.
- **Loudness** matched to the channel: −24 LKFS (CALM Act) by default, tones a set amount above
  the voice.
- **Encoder output quirks:** mono on one side, a little mains hum, and the click of the audio
  switch.
- **Program audio switch or duck** for styles drawn over the live program.
- **Old SD cable look** (4:3, softer chroma) for legacy styles.
- **Channel switch look:** the channel freezes for 1 second, goes black for 1.5 seconds, then
  the alert appears, like a set-top box retuning.
  - **Force-tune option (Motorola style):** pick an "EAS details" channel and viewers are tuned
    there briefly first.
- **Spanish pages** and an optional **"tune to channel"** line.

### ENDEC behaviour
- **Priority system and relay rules**, modelled on the Sage ENDEC filter table:
  - Required Weekly Tests are relayed; Required Monthly Tests are held 5 minutes, then relayed.
  - Custom rules per event code: relay, hold N minutes, log only, or ignore.
- **Channel monitor:** listen to a channel for real SAME tones and relay what it hears. Duplicates
  of alerts already received from IPAWS/NWS are recognised.
- **Scheduled Required Weekly / Monthly Tests**, including a "realistic" random schedule.
- **Custom alert injection** with templates (Civil Emergency Message, Shelter in Place, Boil Water,
  Local Area Emergency and more).

### Text-to-speech
- The bundled **CMU Flite** "kal" voice is the same diphone voice real DASDEC units use for IPAWS
  readouts. Nothing to install.
- Voices: kal (16 kHz and 8 kHz), RMS, SLT. Voice effects: clean ENDEC, NOAA weather radio
  narrowband, weather radio with hiss.
- eSpeak NG, Piper or any custom command (for example a self-installed DECtalk) can be used
  instead.

---

## Screen styles

All previews show the same sample Tornado Warning. The over-the-program styles are drawn over a
test pattern standing in for your channel.

### DASDEC (Digital Alert Systems)

<table>
<tr>
<td align="center" width="50%"><img src="docs/images/styles/dasdec.png" width="400"><br><b>DASDEC — classic</b><br>Purple-blue details page, red outline, Luxi Mono</td>
<td align="center" width="50%"><img src="docs/images/styles/dasdec-legacy.png" width="400"><br><b>DASDEC — 2007–2016</b><br>All-caps "Emergency Alert Details", "Page 1 of 3"</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/dasdec-black.png" width="400"><br><b>DASDEC — black</b><br>Black background</td>
<td align="center"><img src="docs/images/styles/dasdec-plain.png" width="400"><br><b>DASDEC — plain blue</b><br>Arial Bold, no outline</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/dasdec-red.png" width="400"><br><b>DASDEC — red</b><br>Red background</td>
<td align="center"><img src="docs/images/styles/dasdec-redscroll.png" width="400"><br><b>DASDEC + red crawl</b><br>Classic page with a red crawl bar on top</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/dasdec-blackscroll.png" width="400"><br><b>DASDEC + black crawl</b><br>Classic page with a black crawl bar on top</td>
<td></td>
</tr>
</table>

### Trilithic EASyPLUS and EASyCAP

<table>
<tr>
<td align="center" width="50%"><img src="docs/images/styles/easyplus.png" width="400"><br><b>EASyPLUS — classic</b><br>Black screen, scroll under the header</td>
<td align="center" width="50%"><img src="docs/images/styles/easyplus-middle.png" width="400"><br><b>EASyPLUS — middle scroll</b></td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/easyplus-bottom.png" width="400"><br><b>EASyPLUS — bottom scroll</b></td>
<td align="center"><img src="docs/images/styles/easyplus-gray.png" width="400"><br><b>EASyPLUS — grey</b><br>Grey screen, white text</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/easyplus-grayblack.png" width="400"><br><b>EASyPLUS — light grey</b><br>Light grey screen, black text</td>
<td align="center"><img src="docs/images/styles/easyplus-ticker.png" width="400"><br><b>EASyPLUS — ticker</b><br>Ticker only, over the live program</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/easycap.png" width="400"><br><b>EASyCAP</b><br>Blue page, Vera Sans Bold, "- 1 of 2 -"</td>
<td></td>
</tr>
</table>

### Gorman-Redlich, VDS and set-top boxes

<table>
<tr>
<td align="center" width="50%"><img src="docs/images/styles/gorman-redlich.png" width="400"><br><b>Gorman-Redlich EAS-1CG</b><br>"*** EMERGENCY DETAILS ***" pages</td>
<td align="center" width="50%"><img src="docs/images/styles/gorman-redlich-overlay.png" width="400"><br><b>Gorman-Redlich — keyed</b><br>Transparent, over the live program</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/vds-subalert.png" width="400"><br><b>VDS 840 / Sub-Alert</b><br>Colour by alert type, bottom crawl</td>
<td align="center"><img src="docs/images/styles/stb-ticker.png" width="400"><br><b>Set-top box ticker</b><br>Black "EMERGENCY ALERT" screen, red ticker</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/crawl-overlay.png" width="400"><br><b>Crawl over the program</b><br>CG ticker at the top</td>
<td></td>
</tr>
</table>

### SARA set-top firmware (Scientific-Atlanta / Cisco Explorer boxes)

<table>
<tr>
<td align="center" width="50%"><img src="docs/images/styles/sara.png" width="400"><br><b>SARA</b><br>Red paged bar over the program (2010s–today)</td>
<td align="center" width="50%"><img src="docs/images/styles/sara-classic.png" width="400"><br><b>SARA — classic</b><br>Dark-red bar (2000s)</td>
</tr>
<tr>
<td align="center"><img src="docs/images/styles/sara-dasdec.png" width="400"><br><b>SARA + DASDEC</b><br>The box's SARA bar over the headend's DASDEC page</td>
<td></td>
</tr>
</table>

---

## Requirements

- **Dispatcharr.**
- **FFmpeg with libx264.** Dispatcharr already ships a compatible build.
- **Text-to-speech:** nothing to install. Static Flite builds for x86_64 and aarch64 are included
  in `bin/`.

---

## Install

1. Copy the `EmergencyAlertArr` folder into Dispatcharr's `plugins` directory (it's
   self-contained), or install the release zip. Then enable the plugin.
2. Open the plugin settings and work through the numbered sections (see below).
3. Press **Enable EAS**, tune to an armed channel, and press **Test EAS Alert**.

---

## Settings

| Section | What's in it |
|---|---|
| **1 · Channels** | Which channels get alerts: one channel, a group, several groups, or all channels. You can also list groups to skip. |
| **2 · Your area** | Alert source (NWS, IPAWS or both), zone / SAME codes, minimum severity. Also national test mode. |
| **3 · Alert screen** | Screen style, background, old SD look, 4:3 pillarbox or stretch, page and crawl speed, heading, Spanish pages, "tune to" line, operator name, EAS details channel. |
| **4 · Sound** | Generated tones, attention tone length, the alert's own audio, loudness, tone level, audio channels, hum, switch click, program audio (switch / duck / mute), voice, speed and effect. |
| **5 · Tests** | Weekly / monthly / realistic test schedule, day and hour, test area. |
| **6 · Custom alert** | Template, event, message, instruction, sender, severity, duration and area for **Inject Custom Alert**. |
| **7 · Relay rules & monitoring** | RWT / RMT / other alert actions and hold times, custom per-code rules, relaying with your own station ID, and the channel to monitor for SAME tones. |
| **8 · Advanced** | How often to check, maximum alert age, spacing between alerts, station ID, alert video resolution, fonts and other rarely needed options. |

**Tips**

- New alerts are picked up on the next check (every 60 seconds by default).
- On a weak CPU, set **Alert video resolution** (section 8) to 720p.
- Turn **national test mode** off for normal use. It produces a constant stream of alerts from
  across the country.

---

## Actions

| Button | What it does |
|---|---|
| **Enable EAS / Disable EAS** | Arm or unarm the selected channels. |
| **Test EAS Alert** | Send a Required Weekly or Monthly Test to the selected channels that someone is watching right now. If nobody is watching, it tells you. Anyone tuning in within 5 seconds still gets it. |
| **Inject Custom Alert** | The same, with your own alert from section 6. |
| **Stop All Alerts Now** | Panic button: ends everything on screen and marks the current feed as already announced. |
| **View Armed Channels** | Lists armed channels and what each is doing. |
| **View / Clear Alert History** | Recent alerts, tests and injections, including ones that went out with no viewers. |
| **Diagnostics** | Feed status, armed channels with viewers, the text-to-speech engine, the channel monitor, held alerts and more. |
| **Restart Alert Checking / Clean Up Leftover Alert Profiles / Reset Everything** | Housekeeping. |

---

## How the channel switch works

When an alert goes out:

1. The alert screen and sound are built for every watched armed channel.
2. Those channels are switched to the alert. Players are told to retry for about 2–3 seconds while
   Dispatcharr restarts the channel.
3. The plugin follows the alert as it plays. If its stream restarts partway through, the alert
   plays again from the top instead of being lost.
4. Near the end, the channel's normal stream is lined up, so the player goes straight back to the
   channel without reconnecting. Channels using the Proxy profile get a temporary pass-through
   profile until nobody is watching.

---

## Version history

### November 2026

- **0.9.10**
  - The alert follows its own stream. If it restarts in the first seconds, it plays again from
    the top instead of dropping back to the normal channel (fixes "the stream just pauses and
    comes back normal", on every style).
  - More tolerant of players that are slow to reconnect.

### October 2026: the realism rework

- **0.9.9**
  - Test and Inject only fire on watched channels. Before, they built an alert for every armed
    channel, which took minutes and ended in a 504.
  - Cleaner channel switch when several people are watching.
- **0.9.8**
  - No per-channel work when every channel is armed (big cut in CPU and database load).
  - New log line showing how many armed channels are being watched.
- **0.9.7**
  - An alert nobody saw isn't saved for the next channel you flip to.
  - A channel goes back to normal once its viewer leaves.
  - Each alert gets its own time slot.
- **0.9.6**
  - Alerts only go on channels being watched (using Dispatcharr's list of connected players).
  - The 5-second join window.
- **0.9.5**
  - Fixed alerts playing twice (the second time cut off halfway).
- **0.9.4**
  - Seamless return to the channel without a reconnect (fixes TiviMate sticking on the black
    end screen).
- **0.9.3**
  - Fixed settings dropdowns that didn't show up.
- **0.9.2**
  - The channel switch look is fixed (freeze 1 s, black 1.5 s) with no settings.
  - EAS details channel picker.
  - SARA bar shown over the switch.
  - SARA styles play louder.
  - Encoder sound chosen automatically.
  - National test mode.
- **0.9.1**
  - Per-style switch look: DASDEC freeze → black → alert.
- **0.9.0**
  - Loudness matching (−24 LKFS) and encoder output quirks.
  - Program audio duck/switch and the old SD look.
  - Byte-exact encoder headers; more counties in headers.
  - Force-tune / details channel.
  - IPAWS audio.
  - Priority system and relay rules, proper EAN.
  - Channel monitor with duplicate detection.
  - Custom alert templates.
- **0.8.0**
  - ENDEC sound profiles (DASDEC, Sage 3644 / 1822, Trilithic, EASyCAP, TFT, Gorman-Redlich, NWS
    weather radio).
  - Fixes for alerts stuck on screen: config fingerprint, stale-alert skip, spacing between alerts,
    hard deadline, watchdog.
  - New action: Stop All Alerts Now.
- **0.6.1**
  - SARA set-top firmware styles (current and classic).
  - Operator name setting.
- **0.6.0**
  - Bundled CMU Flite text-to-speech with the DASDEC "kal" voice; Piper, eSpeak and custom
    engines.
  - Weather radio voice effects.
- **0.5.0**
  - Researched screen styles: 7 DASDEC, 6 EASyPLUS, EASyCAP, Gorman-Redlich, VDS Sub-Alert,
    set-top ticker, crawl over the program.
  - FCC 2023 wording; correct purge-time rounding.
  - Realistic test schedule.
- **0.4.10**
  - Fast switch-over (alert screen ready in under a second instead of ~30 s).
  - Override-aware profile switching.
  - One lock for all plugin changes.
  - Better duplicate and update handling.
  - Life-safety codes bypass the severity filter.

### July 2026: first public releases

- **SAME decoder update (Jul 8)**
  - SAME decoder, message capture, and relaying of a monitored stream.
- **v0.6.0 (Jul 6)**
  - Webhook notifications (Discord / ntfy / generic) and an FCC-style CSV log.
  - RMT plays the full readout.
  - DASDEC border and EASyPlus crawl fixes.
- **v0.5.0 (Jul 5)**
  - EASyPlus Ticker and DASDEC Scroll styles.
  - EAS-only filter with allow/block lists.
  - IPAWS parser improvements.
- **Jul 4**
  - Initial release, stuck-alert fixes, and the Fetch Alerts Now diagnostic.

The October rework rebuilt the plugin from scratch, so its numbering started over. Webhook
notifications and the CSV log from the July v0.6.0 release aren't in the 0.9 series.

---

## Responsible use

This generates the **real** EAS tones. Keep it on your own private setup. Don't point the test or
inject features at anything public, and don't rebroadcast the tones anywhere they could be
mistaken for an actual alert.
