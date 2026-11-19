# Gesture control (Task 23)

Opt-in. A local camera watches for a few hand poses and maps them to a small
closed set of actions - mainly a hands-free **emergency stop** and **deny**.

| | |
| --- | --- |
| Policy files | `gesture.yaml`, `gestures.yaml` (operator-owned, protected, both default `enabled: false`) |
| Code | `nexus/gesture/` |
| Extra | `pip install "nexus-agent[gesture]"` (MediaPipe, which brings OpenCV); with a current MediaPipe also a hand model file, see "Install" |
| State | none - frames and landmarks live in memory only |
| CLI | `nexus gesture status\|bindings\|classify FILE\|watch [--dry-run]` |
| Tools | none (on purpose: the model must not be able to read or trigger the human's input channel) |

## Install

```bash
pip install "nexus-agent[gesture]"
```

MediaPipe finds the hand. Which of its two hand APIs an installation has
depends on its version, and Nexus works with both:

| MediaPipe | Hand model | What you do |
| --- | --- | --- |
| 0.10.30 and newer, 1.x | not in the package | Download `hand_landmarker.task` once ("HandLandmarker (full)" in the [Models section of Google's Hand Landmarker guide](https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker#models)), put it **outside the workspace**, and set `camera.hand_model` to its absolute path. |
| 0.10.21 and older (Python 3.12 and older only) | built in | Nothing. Leave `camera.hand_model` empty. `pip install "mediapipe<0.10.22"` selects such a version. |

`pip install "nexus-agent[gesture]"` gives you the current MediaPipe, so the
first row is the usual case. `nexus gesture status` says so when the model
file is missing ("this MediaPipe version has no built-in hand model").

The model decides which gesture is seen, so it is trusted like the policy
file: a path inside the workspace is refused, because the agent can write
there. A link, a missing file and a file over 64 MB are refused too.

On a machine without a desktop, OpenCV and MediaPipe need the system
libraries `libGL` and `libEGL`; without them the camera loop stops with "is
installed but does not load".

## Enable

```yaml
# gesture.yaml
schema: 1
enabled: true
camera:
  hand_model: /home/me/models/hand_landmarker.task   # see "Install"; empty for MediaPipe 0.10.21 and older
```

```yaml
# gestures.yaml
schema: 1
enabled: true
bindings:
  fist: emergency_stop
  thumbs_down: deny
  peace: stop_speaking
```

Try it without side effects first:

```bash
nexus gesture watch --dry-run            # prints what would fire
nexus gesture watch --dry-run --poses    # also prints each pose the camera sees, and how sure it is
```

Gestures run in two places:

* `nexus gesture watch` - standalone. Can run `emergency_stop`, `focus_on`,
  `focus_off`. It cannot answer approvals, because those live inside another
  process.
* `nexus voice run` - when gesture control is on, the voice session starts the
  camera loop itself, and gestures can also `deny` / `approve` the prompt that
  session is waiting on, and `stop_speaking`.

## Poses

`open_palm`, `fist`, `thumbs_up`, `thumbs_down`, `peace`, `point_up`.

| Pose | What the camera has to see |
| --- | --- |
| `open_palm` | all five fingers extended, the palm or the back of the hand towards the camera |
| `fist` | all four fingers folded, the thumb tucked in |
| `thumbs_up` / `thumbs_down` | four fingers folded, the thumb out and pointing up / down in the picture |
| `peace` | index and middle finger extended, ring and little finger folded |
| `point_up` | only the index finger extended, pointing up in the picture |

The classifier is plain Python over the 21 hand landmarks
(`nexus/gesture/classify.py`). Ratios of distances decide which fingers are
extended, so it does not depend on hand size, left or right hand, or
mirroring. It uses both things MediaPipe reports: where the joints are in the
picture, and its estimate of the hand in three dimensions. A finger counts as
clearly folded or extended when the two agree; a hand seen from its edge is
not an open palm; a thumb or finger that points at the camera points neither
up nor down. Anything ambiguous is "no pose": an OK sign, three or four
fingers, a "call me" hand, a hand at rest.

`nexus gesture classify FILE` runs the classifier on landmarks in a JSON file:
either 21 `[x, y]` points (one unit for both axes), or
`{"image": [...], "world": [...]}` with MediaPipe's world landmarks as well.

## How a pose becomes an action

1. Confidence must reach `min_confidence`.
2. The pose must be **held** for `hold_ms`, and released before it can fire again.
3. With `arming.required` (default), show the arming pose (`open_palm`) first;
   it arms **one** command for `arming.window_seconds`.
4. `cooldown_ms` after each action, and at most `max_per_minute`.

## Actions and the rules that are not configurable

| Action | Effect |
| --- | --- |
| `emergency_stop` | latches the global emergency stop (same as `nexus stop`) |
| `deny` | refuses the approval the voice session is waiting on |
| `approve` | approves it - only if `approve.enabled: true`, see below |
| `stop_speaking` | interrupts speech (barge-in) |
| `focus_on` / `focus_off` | do-not-disturb |

* `emergency_stop` skips arming, cooldown and the rate limit. **No gesture
  resumes from a stop** - that needs `nexus resume` at a keyboard.
* `approve` is refused when the action is classified **DANGEROUS** (always),
  above `approve.max_risk`, when untrusted content has been read in the session
  (or the session state is unknown), when the session requires speaker or
  second-factor verification, or while the stop is latched. An approval is for
  that one prompt, once.
* Every fired or refused action is audited (`gesture.action`); camera on/off is
  audited (`gesture.camera`).

Approving by gesture is weaker than a spoken, speaker-verified "yes": anyone in
front of the camera can show a thumb. Leave it off unless that is acceptable
for MEDIUM-risk actions in your room.

## Privacy

Frames are captured at low resolution, handed to MediaPipe in memory and
dropped; only the pose name reaches the controller. Nothing is written to
disk, uploaded or logged, and there is no setting that changes this
(`camera.save_frames` is rejected as an unknown setting). The camera's own
activity light is your indicator that the loop is running.

## Known limits

* **What was tried.** The classifier was checked on MediaPipe landmarks of 65
  photographs of hands (`tests/data/gesture_hands.json`): 46 show one of the
  six poses, 19 show something else. All 46 are named right, 45 of them surely
  enough to count at the default `min_confidence`; none of the 19 is taken for
  a pose. These are still photographs, most of them taken against a plain
  background. **No live camera was used**: lighting, motion blur, a cluttered
  background and your own camera angle are untested. Watch your own setup with
  `nexus gesture watch --dry-run --poses` and tune `min_confidence` and
  `hold_ms` before binding anything that matters.
* **Which MediaPipe was tried.** The camera loop was run against MediaPipe
  0.10.21 with both hand APIs (the built-in model, and the Tasks API with a
  model file put together from the models inside that package). With
  MediaPipe 1.2.3 only the setup was checked (the version is recognised, the
  missing model file is reported, the options are accepted); detection was
  not run, and Google's own `hand_landmarker.task` file was never downloaded
  or tried. The camera itself was always a stand-in.
* Without world landmarks (a JSON file with picture points only), an open hand
  seen from its edge still counts as `open_palm`, and far-away hands are less
  sure.
* One hand at a time. `thumbs_up`, `thumbs_down` and `point_up` need the thumb
  or finger within about 35 degrees of straight up or down.
* MediaPipe brings its own OpenCV build (`opencv-contrib-python`). The
  `vision` extra brings another (`opencv-python-headless`). Two OpenCV builds
  in one environment can break `import cv2`; if that happens, uninstall both
  and install one.
* The camera cannot be shared: on most systems the vision pipeline
  (`vision.yaml`) and gesture control cannot hold the same device at once.

## Tests

`pytest tests/test_gesture.py` (offline; no camera or MediaPipe needed: the
camera loop runs against stand-ins for OpenCV and for both MediaPipe APIs, and
the classifier against the landmarks in `tests/data/gesture_hands.json`).
