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
script/run ... --awake-sound sounds/awake.wav --done.wav sounds/done.wav
```

### Change Microphone/Speaker

Run `arecord -L` to list available input devices. Pick devices that start with `plughw:` because they will perform software audio conversions. Use `--mic-device plughw:...` to use a specific input device.

Run `aplay -L` to list available output devices. Pick devices that start with `plughw:` because they will perform software audio conversions. Use `--snd-device plughw:...` to use a specific output device.

### Voice Activity Detection

Use `--vad webrtcvad` to only stream audio when speech is detected.

For better (but slower) speech detection, use [silero VAD](https://github.com/snakers4/silero-vad/) with:

``` sh
.venv/bin/pip3 install .[silerovad]
```

and

``` sh
script/run ... --vad silero
```

### Audio Enhancements

Make use of [webrtc-noise-gain](https://github.com/rhasspy/webrtc-noise-gain) with:

``` sh
.venv/bin/pip3 install .[webrtc]
```

Use `--noise-suppression <NS>` suppress background noise, such as fans (0-4 with 4 being max suppression, default: 0).

Use`--auto-gain <AG>` to automatically increase the microphone volume (0-31 with 31 being the loudest, default: 0).

Use`--volume-multiplier <VM>` to multiply volume by `<VM>` so 2.0 would be twice as loud (default: 1.0).


## Troubleshooting

Add `--debug` to get more information about the messages being exchanged with Home Assistant.

Add `--debug-recording-dir <DIR>` to save recorded audio to a directory `<DIR>`.
