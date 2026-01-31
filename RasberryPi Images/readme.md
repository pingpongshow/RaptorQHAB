
flash the image to your sd card:

Sudo dd if=image.img of=/dev/diskN bs=4m status=progress or use rappberry pi imager

Connect micro USB (use a real micro USB data cable) to your linux/Mac computer, and a new network device should be connected. You should be able to SSH into 10.0.0.1

windows may be different and I don’t have a way to test this

SSH Login
User: raptor
Password: raptor

Expand the  partition to use the full SD card size:

Expand image on new Pi system: “sudo raspi-config --expand-rootfs”

Start airborne unit (temporarily) with : cd /RaptorHAB; python3 -m airborne.main

Start ground unit (temporarily) with : cd /RaptorHAB; python3 -m ground.main

You will need to start the airborne unit or Groundstation as a service to keep it running after disconnecting from SSH

Access the ground station web interface at http://10.0.0.1:5000
