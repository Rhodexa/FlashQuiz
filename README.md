# ⚡ FlashQuiz

A real-time classroom quiz that runs entirely on a local network: **one machine runs the server, one Wi-Fi access point (it can be a phone hotspot), and students join from their phones** by scanning a QR code. You don't need internet, accounts, or an install step on the phones.

```
 [ Projector / laptop ]  ←—— runs server.js, shows /host (main display + controls)
          │
     Wi-Fi AP (router or phone hotspot)
     │     │     │
   phone phone phone     ←—— students open http://<server-ip>:8080/
```

## Run it

It needs **Node.js 16 or newer** and has **zero npm dependencies**.

```bash
git clone <this-repo> flashquiz && cd flashquiz
node server.js                 # or: npm start
```

Then open **http://localhost:8080/host** on the machine connected to the projector.

Options (flags or environment variables):

| Flag            | Env        | Default     | Purpose |
|-----------------|------------|-------------|---------|
| `--port 8080`   | `PORT`     | `8080`      | HTTP port. `--port 80` gives a shorter URL but needs admin/root rights. |
| `--ip 10.0.0.2` | `JOIN_IP`  | auto-detect | Forces the address shown in the QR code. |
|                 | `HOST_KEY` | random      | Key that lets you use `/host?key=…` from another device. |

On startup the console prints the student URL and the host key.

## Networking notes

- **No mDNS:** the QR code and the big URL on screen use the **raw IP address**, so they work on every phone and hotspot.
- **Several network interfaces** (Ethernet + Wi-Fi, VPN, Docker): the server picks the likeliest LAN address. To pick another, open **Network** in the control bar or start with `--ip`.
- **AP is a phone hotspot:** connect the laptop to the hotspot and run the server as usual. In **Network**, enter the hotspot's Wi-Fi name and password. The lobby then shows a **Wi-Fi QR code**, so students can join the network and open the quiz with two scans.
- **Server running on the hotspot phone itself** (e.g. Node in Termux): Android may block interface detection. Start with `--ip 192.168.43.1` (or whatever the hotspot gateway is). If you open `/host?key=…` through the LAN address, the page uses that address automatically.
- **Firewall:** allow incoming TCP on the port. On Fedora: `sudo firewall-cmd --add-port=8080/tcp`. On Windows, accept the prompt the first time it appears.
- **"Client isolation" / "AP isolation":** some routers and hotspots block phones from talking to each other. If phones can't load the page, turn this setting off on the AP.

## Using it in class

1. In the control bar, pick a pack and click **Load**. You can also **Upload…** a `.json` file.
2. Students scan the QR code and type their name. Their names show up in the lobby. Click a name to remove that student.
3. Press **Space** (or the blue button) to start. Each later press does the next step: start → reveal → next question → final results.
4. The answer is revealed when time runs out or when every connected student has answered.
5. **CSV** downloads every student's answers per question, ready for a spreadsheet.

Keyboard: `Space`/`Enter`/`→` runs the main action, `H` hides or shows the controls (for a clean projector view), and `F` toggles fullscreen.

A student who refreshes the page or whose phone screen locks gets back in automatically, with the same name and score.

## Question packs

Put `.json` files in [`packs/`](packs/). They're read each time the list loads, so you don't need to restart the server.

```json
{
  "title": "Biology · Cells",
  "time": 20,
  "speedBonus": true,
  "questions": [
    { "q": "Powerhouse of the cell?", "choices": ["Nucleus", "Mitochondria", "Ribosome", "Golgi"], "answer": 1 },
    { "q": "Plant cells have a cell wall.", "choices": ["True", "False"], "answer": 0, "time": 10 }
  ]
}
```

- `choices`: 2–4 options. `answer`: the **0-based index** of the correct choice.
- `time`: seconds per question (5–300). You can set it for the whole pack and override it on any question.
- `speedBonus`: `true` scores 500 points for a correct answer plus up to 500 for answering fast. `false` scores a flat 500, which suits graded tests.
- Packs you don't want tracked in git can go in `packs/private/`, which git ignores.

## Design choices

- **Transport:** Server-Sent Events going down and small POST requests going up. SSE works in every phone browser and reconnects by itself after a phone sleeps, and it doesn't need a WebSocket library. The host display gets at most about 8 updates per second, even if the whole class answers at once.
- **Colorblind-safe:** choices use the Okabe-Ito palette, and each one also has a **shape and a letter** (▲A ◆B ●C ■D). "Correct" and "wrong" are shown with ✓/✗ and words, never with color alone, and red/green is never used as a pair.
- **Low-end devices:** no frameworks, web fonts, shadows, or blur. The only animation is one CSS transform for the timer bar. The QR codes are SVGs generated on the server ([`lib/qr.js`](lib/qr.js)) and work offline.
- **Host protection:** control endpoints only accept requests from the server machine (localhost) or ones that carry the host key, so students can't press "Next".

## Project layout

```
server.js          HTTP server, game state, SSE, host API, CSV export
lib/qr.js          dependency-free QR encoder (byte mode, ECC M, v1–10)
public/host.html   main display + control panel
public/play.html   student client
public/common.css  shared styles / palette
packs/*.json       question packs
```

## Status / next ideas

This is a first mockup. Things to consider next: images in questions, shuffling the answer order for each student, multiple-answer questions, saving finished sessions to disk, and a teacher remote view for phones (`/host?key=…` already works there but isn't tuned for small screens).
