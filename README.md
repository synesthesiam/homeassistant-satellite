# Home Assistant Satellite

Python-based satellite for [Assist](https://www.home-assistant.io/voice_control/) that streams audio to Home Assistant from a microphone.

You must have the [openWakeWord add-on](https://my.home-assistant.io/redirect/supervisor_addon/?addon=47701997_openwakeword&repository_url=https%3A%2F%2Fgithub.com%2Frhasspy%2Fhassio-addons) installed.


## Requirements

* Python 3.9 or higher
* ffmpeg
* alsa-utils (for `arecord` and `aplay`)


## Installation

Install Python and the required system dependencies:

``` sh
sudo apt-get update
sudo apt-get install python3 python3-pip python3-venv \
                     alsa-utils git

sudo apt-get install --no-install-recommends \
                     ffmpeg
```

Clone the repository and run the setup script:

``` sh
git clone https://github.com/synesthesiam/homeassistant-satellite.git
cd homeassistant-satellite
script/setup
```

This will create a virtual environment and install the package.

## Long-Lived Access Token

You must create a long-lived access token in Home Assistant for the satellite to access the websocket API.

1. Go to your profile page in Home Assistant
2. Scroll down to "Long-lived access tokens"
3. Click "Create token"
4. Enter a name and click "OK"
5. Copy the **entire token** using the copy button provided
6. Save the token somewhere you can paste from later


## Running

``` sh
script/run --host <IP> --token <TOKEN>
```

where `<IP>` is the IP address of your Home Assistant server and `<TOKEN>` is the long-lived access token.

This will stream audio from the default microphone to your preferred pipeline in Home Assistant.

See `--help` for more options

### Feedback Sounds

Use `--awake-sound <WAV>` and `--done-sound <WAV>` to play sounds when the wake word is detected and when a voice command is finished.

For example:

``` sh
script/run ... --awake-sound sounds/awake.wav --done-sound sounds/done.wav
```

### Change Microphone/Speaker

Run `arecord -L` to list available input devices. Pick devices that start with `plughw:` because they will perform software audio conversions. Use `--mic-device plughw:...` to use a specific input device.

Run `aplay -L` to list available output devices. Pick devices that start with `plughw:` because they will perform software audio conversions. Use `--snd-device plughw:...` to use a specific output device.

### Voice Activity Detection

For fast but inaccurate speech detection:

``` sh
.venv/bin/pip3 install .[webrtc]
```

and

``` sh
script/run ... --vad webrtcvad
```

For much better (but slower) speech detection, use [silero VAD](https://github.com/snakers4/silero-vad/) with:

``` sh
.venv/bin/pip3 install \
  --find-links https://synesthesiam.github.io/prebuilt-apps/ \
  .[silerovad]
```

and

``` sh
script/run ... --vad silero
```

**NOTE:** The `--find-links` option is only necessary on 32-bit ARM systems because Microsoft does not build `onnxruntime` wheels for them.

### Audio Enhancements

Make use of [webrtc-noise-gain](https://github.com/rhasspy/webrtc-noise-gain) with:

``` sh
.venv/bin/pip3 install .[webrtc]
```

Use `--noise-suppression <NS>` suppress background noise, such as fans (0-4 with 4 being max suppression, default: 0).

Use`--auto-gain <AG>` to automatically increase the microphone volume (0-31 with 31 being the loudest, default: 0).

Use`--volume-multiplier <VM>` to multiply volume by `<VM>` so 2.0 would be twice as loud (default: 1.0).


## Running as a Service

You can run homeassistant-satellite as a systemd service by first creating a service file:

``` sh
sudo systemctl edit --force --full homeassistant-satellite.service
```

Paste in the following template, and change both `/home/pi/homeassistant-satellite` and the `script/run` arguments to match your set up:

``` text
[Unit]
Description=Home Assistant Satellite

[Service]
Type=simple
ExecStart=/home/pi/homeassistant-satellite/script/run --host <host> --token <token>
WorkingDirectory=/home/pi/homeassistant-satellite
Restart=always
RestartSec=1

[Install]
WantedBy=default.target
```

Save the file and exit your editor. Next, enable the service to start at boot and run it:

``` sh
sudo systemctl enable --now homeassistant-satellite.service
```

(you may need to hit CTRL+C to get back to a shell prompt)

With the service running, you can view logs in real-time with:

``` sh
journalctl -u homeassistant-satellite.service -f
```

Disable and stop the service with:

``` sh
sudo systemctl disable --now homeassistant-satellite.service
```


### Running in docker

cmd:

```bash
docker run --rm -ti --name "homeassistant-satellite" --device /dev/snd --group-add=audio \
    ghcr.io/synesthesiam/homeassistant-satellite:latest \
    --host <HOST> \
    --token <TOCKEN> \
    ...
```

compose:

```yaml
version: "3.8"
services:
  homeassistant-satellite:
    image: "ghcr.io/synesthesiam/homeassistant-satellite:latest"
    devices:
      - /dev/snd:/dev/snd
    group_add:
      - audio
    command:
      - --host
      - <HOST>
      - --token
      - <TOCKEN>
```

## Troubleshooting

Add `--debug` to get more information about the messages being exchanged with Home Assistant.

Add `--debug-recording-dir <DIR>` to save recorded audio to a directory `<DIR>`.
