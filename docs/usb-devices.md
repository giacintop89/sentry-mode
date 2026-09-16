# USB devices and links

What a USB camera can do is decided by the link it is plugged into, not by the settings
asked of it. The same StreamCam, unchanged, offers different modes on a USB 2 port and a
USB 3 one, and a failing neighbour on the bus can take it off the air entirely. This page
records the behaviour seen on a Pi 5 and the commands that identify it.

## The link decides the modes

Measured on one Logitech StreamCam at 1920x1080, moving nothing but the cable:

| | USB 2 (480M) | USB 3 (5000M) |
|---|---|---|
| Highest mode advertised at 1080p | MJPG 30 fps | MJPG 60 fps |
| Uncompressed YUYV at 1080p | 5 fps | 5 fps |
| `effective_fps` with YUYV | 2.5 | — |
| `effective_fps` with MJPG | 15.0 | 29.5 |

Two lessons. Asking for an uncompressed format costs most of the rate — 1080p YUYV is
4,147,200 bytes a frame, and the link cannot carry many of them — which is why
`camera.fourcc` defaults to `MJPG` ([camera and video](camera-and-video.md)). And a port
change can add modes that were never listed before: the 60 fps entries appeared only on the
SuperSpeed port.

List what the device really offers, rather than assuming:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
v4l2-ctl -d /dev/video0 --get-fmt-video      # what is negotiated right now
lsusb -t                                     # 480M or 5000M, and the bound driver
```

To separate the device from this software, read frames without it. If this reports a healthy
rate and the node does not, the problem is above the driver:

```bash
v4l2-ctl -d /dev/video0 --set-fmt-video=width=1280,height=720,pixelformat=MJPG \
  --set-parm=60 --stream-mmap --stream-count=120
```

## Symptoms and what they mean

| What you see | What it is |
|---|---|
| `camera returned no frame` moments after a successful open, with `VIDIOC_REQBUFS: errno=19 (No such device)` | The device re-enumerated. The open handle points at nothing and stays broken until it is reopened. |
| `WARN: Max Exit Latency too large`, `Could not enable U1 link state, xHCI error -22`, then `USB disconnect` | USB 3 link power management the device cannot negotiate. It drops off the bus as soon as it is asked to stream. |
| `uas_eh_abort_handler`, `uas_eh_device_reset_handler`, `I/O error, dev sda` | A USB-SATA bridge misbehaving under the `uas` driver. Often blamed on the disk; usually the adapter. |
| A device number that climbs steadily (`new SuperSpeed USB device number 31, 32, 33…`) | Something is re-enumerating in a loop. On a Pi 5 both USB 3 ports share one RP1 controller, so a flapping device disturbs the other port too. |

Count the churn rather than eyeballing it:

```bash
sudo dmesg | grep -c "usb 4-1: new SuperSpeed"   # one device's re-enumerations
sudo dmesg | grep -E "usb 4-1:|uvcvideo" | tail
```

## Quirks

Both problems above are fixed by telling the kernel to treat one device specially. Quirks
are matched by vendor and product id, so they affect nothing else.

| Quirk | Flag | What it stops |
|---|---|---|
| `usbcore.quirks=046d:0893:k` | `k` = no LPM | The USB 3 link power states a StreamCam cannot negotiate. |
| `usb-storage.quirks=2537:1066:u` | `u` = ignore UAS | UAS on a Norelsys NS1066 bridge; it falls back to plain USB storage. |

Apply either without rebooting. The value takes effect the next time the device is probed,
so force a re-probe by unbinding and rebinding it — a device already bound keeps its old
behaviour, and a device that merely resets is not re-probed either:

```bash
echo '046d:0893:k'  | sudo tee /sys/module/usbcore/parameters/quirks
echo '2537:1066:u'  | sudo tee /sys/module/usb_storage/parameters/quirks
echo 4-1 | sudo tee /sys/bus/usb/drivers/usb/unbind     # the device's bus-port id
echo 4-1 | sudo tee /sys/bus/usb/drivers/usb/bind
```

Runtime values are lost at reboot. To keep them, append both to the single line in
`/boot/firmware/cmdline.txt` — back it up first, and keep it one line:

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.backup
sudo sed -i '1s|$| usbcore.quirks=046d:0893:k usb-storage.quirks=2537:1066:u|' \
  /boot/firmware/cmdline.txt
```

Verify after the next boot with `lsusb -t`: the bridge should read `Driver=usb-storage`
rather than `Driver=uas`, and the camera should stream without disconnecting.

Measured effect of the storage quirk on one node: re-enumerations went from roughly one a
second to none in 45 seconds of watching, the camera stopped being knocked off the bus, and
the disk — a healthy Samsung SSD behind the bridge — probed cleanly for the first time.

## What the node does about it

A capture that loses its device does not end the session. Each read is retried up to six
times, a second apart, closing and reopening the camera between attempts, because a
re-enumerated device needs the handle reopened rather than the read repeated. Recoveries are
counted as `reconnects` in GET `/api/video/status`; a rising count with the preview still
running means the bus is unstable but the node is coping. When every attempt fails, the
capture stops with the real error — `cannot open camera …` — instead of a stale one.

Retries buy seconds, not minutes. A device that disappears for longer than that is a
hardware problem: check the port, the cable, the power, and any other device sharing the
controller.

See also: [camera and video](camera-and-video.md),
[hardware and devices](hardware-and-devices.md), [Raspberry Pi setup](raspberry-pi-setup.md).
