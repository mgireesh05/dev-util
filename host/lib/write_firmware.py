# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import binascii
import glob
import os
import re
import struct
import time
from tools import CmdError

def RoundUp(value, boundary):
  """Align a value to the next power of 2 boundary.

  Args:
    value: The value to align.
    boundary: The boundary value, e.g. 4096. Must be a power of 2.

  Returns:
    The rounded-up value.
  """
  return (value + boundary - 1) & ~(boundary - 1)


class WriteFirmware:
  """Write firmware to a Tegra 2 board using USB A-A cable.

  This class handles re-reflashing a board with new firmware using the Tegra's
  built-in boot ROM feature. This works by putting the chip into a special mode
  where it ignores any available firmware and instead reads it from a connected
  host machine over USB.

  In our case we use that feature to send U-Boot along with a suitable payload
  and instructions to flash it to SPI flash. The payload is itself normally a
  full Chrome OS image consisting of U-Boot, some keys and verification
  information, images and a map of the flash memory.

  Private attributes:
    _servo_port: Port number to use to talk to servo with dut-control.
      Special values are:
        None: servo is not available.
        0: any servo will do.

  """
  def __init__(self, tools, fdt, output, bundle):
    """Set up a new WriteFirmware object.

    Args:
      tools: A tools library for us to use.
      fdt: An fdt which gives us some info that we need.
      output: An output object to use for printing progress and messages.
      bundle: A BundleFirmware object which created the image.
    """
    self._tools = tools
    self._fdt = fdt
    self._out = output
    self._bundle = bundle
    self.text_base = self._fdt.GetInt('/chromeos-config', 'textbase', -1)

    # For speed, use the 'update' algorithm and don't verify
    self.update = True
    self.verify = False

    # Use default servo port
    self._servo_port = 0

  def SelectServo(self, servo):
    """Select the servo to use for writing firmware.

    Args:
      servo: String containing description of servo to use:
        'none'  : Don't use servo, generate an error on any attempt.
        'any'   : Use any available servo.
        '<port>': Use servo with that port number.
    """
    if servo == 'none':
      self._servo_port = None
    elif servo == 'any':
      self._servo_port = 0
    else:
      self._servo_port = int(servo)
    self._out.Notice('Servo port %s' % str(self._servo_port))

  def _GetFlashScript(self, payload_size, update, verify, boot_type, checksum,
                      bus='0'):
    """Get the U-Boot boot command needed to flash U-Boot.

    We leave a marker in the string for the load address of the image,
    since this depends on the size of this script. This can be replaced by
    the caller provided that the marker length is unchanged.

    Args:
      payload_size: Size of payload in bytes.
      update: Use faster update algorithm rather then full device erase
      verify: Verify the write by doing a readback and CRC
      boot_type: The source for bootdevice (nand, sdmmc, or spi)
      checksum: The checksum of the payload (an integer)
      bus: The bus number

    Returns:
      A tuple containing:
        The script, as a string ready to use as a U-Boot boot command, with an
            embedded marker for the load address.
        The marker string, which the caller should replace with the correct
            load address as 8 hex digits, without changing its length.
    """
    replace_me = 'zsHEXYla'
    page_size = 4096
    if boot_type == 'sdmmc':
      page_size = 512
    if boot_type != 'spi':
      update = False

    cmds = [
        'setenv address       0x%s' % replace_me,
        'setenv firmware_size %#x' % payload_size,
        'setenv length        %#x' % RoundUp(payload_size, page_size),
        'setenv blocks   %#x' % (RoundUp(payload_size, page_size) / page_size),
        'setenv _crc    "crc32 -v ${address} ${firmware_size} %#08x"' %
            checksum,
        'setenv _clear  "echo Clearing RAM; mw.b     ${address} 0 ${length}"',
    ]
    if boot_type == 'nand':
      cmds.extend([
          'setenv _init   "echo Init NAND;  nand info"',
          'setenv _erase  "echo Erase NAND; nand erase            0 ${length}"',
          'setenv _write  "echo Write NAND; nand write ${address} 0 ${length}"',
          'setenv _read   "echo Read NAND;  nand read  ${address} 0 ${length}"',
      ])
    elif boot_type == 'sdmmc':
      cmds.extend([
          'setenv _init   "echo Init EMMC;  mmc rescan            0"',
          'setenv _erase  "echo Erase EMMC; "',
          'setenv _write  "echo Write EMMC; mmc write 0 ${address} 0 ' \
             '${blocks} boot1"',
          'setenv _read   "echo Read EMMC;  mmc read 0 ${address} 0 ' \
             '${blocks} boot1"',
      ])
    else:
      cmds.extend([
          'setenv _init   "echo Init SPI;   sf probe            %s"' % bus,
          'setenv _erase  "echo Erase SPI;  sf erase            0 ${length}"',
          'setenv _write  "echo Write SPI;  sf write ${address} 0 ${length}"',
          'setenv _read   "echo Read SPI;   sf read  ${address} 0 ${length}"',
          'setenv _update "echo Update SPI; sf update ${address} 0 ${length}"',
      ])

    cmds.extend([
        'echo Firmware loaded to ${address}, size ${firmware_size}, '
            'length ${length}',
        'if run _crc; then',
        'run _init',
    ])
    if update:
      cmds += ['time run _update']
    else:
      cmds += ['run _erase', 'run _write']
    if verify:
      cmds += [
        'run _clear',
        'run _read',
        'run _crc',
      ]
    else:
      cmds += ['echo Skipping verify']
    cmds.extend([
      'else',
      'echo',
      'echo "** Checksum error on load: please check download tool **"',
      'fi',
      ])
    script = '; '.join(cmds)
    return script, replace_me

  def PrepareFlasher(self, uboot, payload, update, verify, boot_type, bus):
    """Get a flasher ready for sending to the board.

    The flasher is an executable image consisting of:

      - U-Boot (u-boot.bin);
      - a special FDT to tell it what to do in the form of a run command;
      - (we could add some empty space here, in case U-Boot is not built to
          be relocatable);
      - the payload (which is a full flash image, or signed U-Boot + fdt).

    Args:
      uboot: Full path to u-boot.bin.
      payload: Full path to payload.
      update: Use faster update algorithm rather then full device erase
      verify: Verify the write by doing a readback and CRC
      boot_type: the src for bootdevice (nand, sdmmc, or spi)

    Returns:
      Filename of the flasher binary created.
    """
    fdt = self._fdt.Copy(os.path.join(self._tools.outdir, 'flasher.dtb'))
    payload_data = self._tools.ReadFile(payload)

    # Make sure that the checksum is not negative
    checksum = binascii.crc32(payload_data) & 0xffffffff

    script, replace_me = self._GetFlashScript(len(payload_data), update,
                                              verify, boot_type, checksum, bus)
    data = self._tools.ReadFile(uboot)
    fdt.PutString('/config', 'bootcmd', script)
    fdt_data = self._tools.ReadFile(fdt.fname)

    # Work out where to place the payload in memory. This is a chicken-and-egg
    # problem (although in case you haven't heard, it was the chicken that
    # came first), so we resolve it by replacing the string after
    # fdt.PutString has done its job.
    #
    # Correction: Technically, the egg came first. Whatever genetic mutation
    # created the new species would have been present in the egg, but not the
    # parent (since if it was in the parent, it would have been present in the
    # parent when it was an egg).
    #
    # Question: ok so who laid the egg then?
    payload_offset = len(data) + len(fdt_data)

    # NAND driver expects 4-byte alignment.  Just go whole hog and do 4K.
    alignment = 0x1000
    payload_offset = (payload_offset + alignment - 1) & ~(alignment - 1)

    load_address = self.text_base + payload_offset,
    new_str = '%08x' % load_address
    if len(replace_me) is not len(new_str):
      raise ValueError("Internal error: replacement string '%s' length does "
          "not match new string '%s'" % (replace_me, new_str))
    matches = len(re.findall(replace_me, fdt_data))
    if matches != 1:
      raise ValueError("Internal error: replacement string '%s' already "
          "exists in the fdt (%d matches)" % (replace_me, matches))
    fdt_data = re.sub(replace_me, new_str, fdt_data)

    # Now put it together.
    data += fdt_data
    data += "\0" * (payload_offset - len(data))
    data += payload_data
    flasher = os.path.join(self._tools.outdir, 'flasher-for-image.bin')
    self._tools.WriteFile(flasher, data)

    # Tell the user about a few things.
    self._tools.OutputSize('U-Boot', uboot)
    self._tools.OutputSize('Payload', payload)
    self._out.Notice('Payload checksum %08x' % checksum)
    self._tools.OutputSize('Flasher', flasher)
    return flasher

  def NvidiaFlashImage(self, flash_dest, uboot, bct, payload, bootstub):
    """Flash the image to SPI flash.

    This creates a special Flasher binary, with the image to be flashed as
    a payload. This is then sent to the board using the tegrarcm utility.

    Args:
      flash_dest: Destination for flasher, or None to not create a flasher
          Valid options are spi, sdmmc
      uboot: Full path to u-boot.bin.
      bct: Full path to BCT file (binary chip timings file for Nvidia SOCs).
      payload: Full path to payload.
      bootstub: Full path to bootstub, which is the payload without the
          signing information (i.e. bootstub is u-boot.bin + the FDT)

    Returns:
      True if ok, False if failed.
    """
    # Use a Regex to pull Boot type from BCT file.
    match = re.compile('DevType\[0\] = NvBootDevType_(?P<boot>([a-zA-Z])+);')
    bct_dumped = self._tools.Run('bct_dump', [bct]).splitlines()

    # TODO(sjg): The boot type is currently selected by the bct, rather than
    # flash_dest selecting which bct to use. This is a bit backwards. For now
    # we go with the bct's idea.
    boot_type = filter(match.match, bct_dumped)
    boot_type = match.match(boot_type[0]).group('boot').lower()

    if flash_dest:
      image = self.PrepareFlasher(uboot, payload, self.update, self.verify,
                                    boot_type, 0)
    elif bootstub:
      image = bootstub

    else:
      image = payload
      # If we don't know the textbase, extract it from the payload.
      if self.text_base == -1:
        data = self._tools.ReadFile(payload)
        # Skip the BCT which is the first 64KB
        self.text_base = self._bundle.DecodeTextBase(data[0x10000:])

    self._out.Notice('TEXT_BASE is %#x' % self.text_base)
    self._out.Progress('Uploading flasher image')
    args = [
      '--bct', bct,
      '--bootloader',  image,
      '--loadaddr', "%#x" % self.text_base
    ]

    # TODO(sjg): Check for existence of board - but chroot has no lsusb!
    last_err = None
    for _ in range(10):
      try:
        # TODO(sjg): Use Chromite library so we can monitor output
        self._tools.Run('tegrarcm', args, sudo=True)
        self._out.Notice('Flasher downloaded - please see serial output '
            'for progress.')
        return True

      except CmdError as err:
        if not self._out.stdout_is_tty:
          return False

        # Only show the error output once unless it changes.
        err = str(err)
        if not 'could not open USB device' in err:
          raise CmdError('tegrarcm failed: %s' % err)

        if err != last_err:
          self._out.Notice(err)
          last_err = err
          self._out.Progress('Please connect USB A-A cable and do a '
              'recovery-reset', True)
        time.sleep(1)

    return False

  def _WaitForUSBDevice(self, name, vendor_id, product_id, timeout=10):
    """Wait until we see a device on the USB bus.

    Args:
      name: Board type name
      vendor_id: USB vendor ID to look for
      product_id: USB product ID to look for
      timeout: Timeout to wait in seconds

    Returns
      True if the device was found, False if we timed out.
    """
    self._out.Progress('Waiting for board to appear on USB bus')
    start_time = time.time()
    while time.time() - start_time < timeout:
      try:
        args = ['-d', '%04x:%04x' % (vendor_id, product_id)]
        self._tools.Run('lsusb', args, sudo=True)
        self._out.Progress('Found %s board' % name)
        return True

      except CmdError:
        pass

    return False

  def _DutControl(self, args):
    """Run dut-control with supplied arguments.

    The correct servo will be used based on self._servo_port.

    Args:
      args: List of arguments to dut-control.

    Retruns:
      a string, stdout generated by running the command
    Raises:
      IOError if no servo access is permitted.
    """
    if self._servo_port is None:
      raise IOError('No servo access available, please use --servo')
    if self._servo_port:
      args.extend(['-p', '%s' % self._servo_port])
    return self._tools.Run('dut-control', args)

  def _ExtractPayloadParts(self, payload):
    """Extract the BL1, BL2 and U-Boot parts from a payload.

    An exynos image consists of 3 parts: BL1, BL2 and U-Boot/FDT.

    This pulls out the various parts, puts them into files and returns
    these files.

    Args:
      payload: Full path to payload.

    Returns:
      (bl1, bl2, image) where:
        bl1 is the filename of the extracted BL1
        bl2 is the filename of the extracted BL2
        image is the filename of the extracted U-Boot image
    """
    # Pull out the parts from the payload
    bl1 = os.path.join(self._tools.outdir, 'bl1.bin')
    bl2 = os.path.join(self._tools.outdir, 'bl2.bin')
    image = os.path.join(self._tools.outdir, 'u-boot-from-image.bin')
    data = self._tools.ReadFile(payload)

    # The BL1 is always 8KB - extract that part into a new file
    # TODO(sjg@chromium.org): Perhaps pick these up from the fdt?
    bl1_size = 0x2000
    self._tools.WriteFile(bl1, data[:bl1_size])

    # Try to detect the BL2 size. We look for 0xea000014 which is the
    # 'B reset' instruction at the start of U-Boot.
    first_instr = struct.pack('<L', 0xea000014)
    uboot_offset = data.find(first_instr, bl1_size + 0x3800)
    if uboot_offset == -1:
      raise ValueError('Could not locate start of U-Boot')
    bl2_size = uboot_offset - bl1_size - 0x800  # 2KB gap after BL2

    # Sanity check: At present we only allow 14KB and 30KB for SPL
    allowed = [14, 30]
    if (bl2_size >> 10) not in allowed:
      raise ValueError('BL2 size is %dK - only %s supported' %
                       (bl2_size >> 10, ', '.join(
            [str(size) for size in allowed])))
    self._out.Notice('BL2 size is %dKB' % (bl2_size >> 10))

    # The BL2 (U-Boot SPL) follows BL1. After that there is a 2KB gap
    bl2_end = uboot_offset - 0x800
    self._tools.WriteFile(bl2, data[0x2000:bl2_end])

    # U-Boot itself starts after the gap
    self._tools.WriteFile(image, data[uboot_offset:])
    return bl1, bl2, image

  def ExynosFlashImage(self, flash_dest, flash_uboot, bl1, bl2, payload,
                        kernel):
    """Flash the image to SPI flash.

    This creates a special Flasher binary, with the image to be flashed as
    a payload. This is then sent to the board using the tegrarcm utility.

    Args:
      flash_dest: Destination for flasher, or None to not create a flasher
          Valid options are spi, sdmmc.
      flash_uboot: Full path to u-boot.bin to use for flasher.
      bl1: Full path to file containing BL1 (pre-boot).
      bl2: Full path to file containing BL2 (SPL).
      payload: Full path to payload.
      kernel: Kernel to send after the payload, or None.

    Returns:
      True if ok, False if failed.
    """
    if flash_dest:
      image = self.PrepareFlasher(flash_uboot, payload, self.update,
                                  self.verify, flash_dest, '1:0')
    else:
      bl1, bl2, image = self._ExtractPayloadParts(payload)

    vendor_id = 0x04e8
    product_id = 0x1234

    # Preserve dut_hub_sel state.
    preserved_dut_hub_sel = self._DutControl(['dut_hub_sel',]
                                             ).strip().split(':')[-1]
    required_dut_hub_sel = 'dut_sees_servo'
    args = ['warm_reset:on', 'fw_up:on', 'pwr_button:press', 'sleep:.1',
        'warm_reset:off']
    if preserved_dut_hub_sel != required_dut_hub_sel:
      # Need to set it to get the port properly powered up.
      args += ['dut_hub_sel:%s' % required_dut_hub_sel]
    # TODO(sjg) If the board is bricked a reset does not seem to bring it
    # back to life.
    # BUG=chromium-os:28229
    args = ['cold_reset:on', 'sleep:.2', 'cold_reset:off'] + args
    self._out.Progress('Reseting board via servo')
    self._DutControl(args)

    # If we have a kernel to write, create a new image with that added.
    if kernel:
      dl_image = os.path.join(self._tools.outdir, 'image-plus-kernel.bin')
      data = self._tools.ReadFile(image)

      # Pad the original payload out to the original length
      data += '\0' * (os.stat(payload).st_size - len(data))
      data += self._tools.ReadFile(kernel)
      self._tools.WriteFile(dl_image, data)
    else:
      dl_image = image

    self._out.Progress('Uploading image')
    download_list = [
        # The numbers are the download addresses (in SRAM) for each piece
        # TODO(sjg@chromium.org): Perhaps pick these up from the fdt?
        ['bl1', 0x02021400, bl1],
        ['bl2', 0x02023400, bl2],
        ['u-boot', 0x43e00000, dl_image]
        ]
    try:
      for upto in range(len(download_list)):
        item = download_list[upto]
        if not self._WaitForUSBDevice('exynos', vendor_id, product_id, 4):
          if upto == 0:
            raise CmdError('Could not find Exynos board on USB port')
          raise CmdError("Stage '%s' did not complete" % item[0])
        self._out.Notice(item[2])
        self._out.Progress("Uploading stage '%s'" % item[0])

        if upto == 0:
          # The IROM needs roughly 200ms here to be ready for USB download
          time.sleep(.5)

        args = ['-a', '%#x' % item[1], '-f', item[2]]
        self._tools.Run('smdk-usbdl', args, sudo=True)
        if upto == 1:
          # Once SPL starts up we can release the power buttom
          args = ['fw_up:off', 'pwr_button:release']
          self._DutControl(args)

    finally:
      # Make sure that the power button is released and dut_sel_hub state is
      # restored, whatever happens
      args = ['fw_up:off', 'pwr_button:release']
      if preserved_dut_hub_sel != required_dut_hub_sel:
        args += ['dut_hub_sel:%s' % preserved_dut_hub_sel]
      self._DutControl(args)

    self._out.Notice('Image downloaded - please see serial output '
        'for progress.')
    return True

  def _GetDiskInfo(self, disk, item):
    """Returns information about a SCSI disk device.

    Args:
      disk: a block device name in sys/block, like '/sys/block/sdf'.
      item: the item of disk information that is required.

    Returns:
      The information obtained, as a string, or '[Unknown]' if not found
    """
    dev_path = os.path.join(disk, 'device')

    # Search upwards and through symlinks looking for the item.
    while os.path.isdir(dev_path) and dev_path != '/sys':
      fname = os.path.join(dev_path, item)
      if os.path.exists(fname):
        with open(fname, 'r') as fd:
          return fd.readline().rstrip()

      # Move up a level and follow any symlink.
      new_path = os.path.join(dev_path, '..')
      if os.path.islink(new_path):
        new_path = os.path.abspath(os.readlink(os.path.dirname(dev_path)))
      dev_path = new_path
    return '[Unknown]'

  def _GetDiskCapacity(self, device):
    """Returns the disk capacity in GB, or 0 if not known.

    Args:
      device: Device to check, like '/dev/sdf'.

    Returns:
      Capacity of device in GB, or 0 if not known.
    """
    args = ['-l', device]
    stdout = self._tools.Run('fdisk', args, sudo=True)
    if stdout:
      # Seach for the line with capacity information.
      re_capacity = re.compile('Disk .*: (\d+) \w+,')
      lines = filter(re_capacity.match, stdout.splitlines())
      if len(lines):
        m = re_capacity.match(lines[0])

        # We get something like 7859 MB, so turn into bytes, then GB
        return int(m.group(1)) * 1024 * 1024 / 1e9
    return 0

  def _ListUsbDisks(self):
    """Return a list of available removable USB disks.

    Returns:
      List of USB devices, each element is itself a list containing:
        device ('/dev/sdx')
        manufacturer name
        product name
        capacity in GB (an integer)
        full description (all of the above concatenated).
    """
    disk_list = []
    for disk in glob.glob('/sys/block/sd*'):
      with open(disk + '/removable', 'r') as fd:
        if int(fd.readline()) == 1:
          device = '/dev/%s' % disk.split('/')[-1]
          manuf = self._GetDiskInfo(disk, 'manufacturer')
          product = self._GetDiskInfo(disk, 'product')
          capacity = self._GetDiskCapacity(device)
          if capacity:
            desc = '%s: %s %s %d GB' % (device, manuf, product, capacity)
            disk_list.append([device, manuf, product, capacity, desc])
    return disk_list

  def WriteToSd(self, flash_dest, disk, uboot, payload):
    if flash_dest:
      raw_image = self.PrepareFlasher(uboot, payload, self.update, self.verify,
                                  flash_dest, '1:0')
      bl1, bl2, _ = self._ExtractPayloadParts(payload)
      spl_load_size = os.stat(raw_image).st_size
      bl2 = self._bundle.ConfigureExynosBl2(self._fdt, spl_load_size, bl2,
                                            'flasher')

      data = self._tools.ReadFile(bl1) + self._tools.ReadFile(bl2)

      # Pad BL2 out to the required size.
      # We require that it be 24KB, but data will only contain 8KB + 14KB.
      # Add the extra padding to bring it to 24KB.
      data += '\0' * (0x6000 - len(data))
      data += self._tools.ReadFile(raw_image)
      image = os.path.join(self._tools.outdir, 'flasher-with-bl.bin')
      self._tools.WriteFile(image, data)
      self._out.Progress('Writing flasher to %s' % disk)
    else:
      image = payload
      self._out.Progress('Writing image to %s' % disk)

    args = ['if=%s' % image, 'of=%s' % disk, 'bs=512', 'seek=1']
    self._tools.Run('dd', args, sudo=True)

  def SendToSdCard(self, dest, flash_dest, uboot, payload):
    """Write a flasher to an SD card.

    Args:
      dest: Destination in one of these forms:
          ':<full description of device>'
          ':.' selects the only available device, fails if more than one option
          ':<device>' select deivce

          Examples:
            ':/dev/sdd: Generic Flash Card Reader/Writer 8 GB'
            ':.'
            ':/dev/sdd'

      flash_dest: Destination for flasher, or None to not create a flasher:
          Valid options are spi, sdmmc.
      uboot: Full path to u-boot.bin.
      payload: Full path to payload.
    """
    disk = None
    disks = self._ListUsbDisks()
    if dest[:1] == ':':
      name = dest[1:]

      # A '.' just means to use the only available disk.
      if name == '.' and len(disks) == 1:
        disk = disks[0][0]
      for disk_info in disks:
        # Use the full name or the device name.
        if disk_info[4] == name or disk_info[1] == name:
          disk = disk_info[0]

    if disk:
      self.WriteToSd(flash_dest, disk, uboot, payload)
    else:
      self._out.Error("Please specify destination -w 'sd:<disk_description>':")
      self._out.Error('   - description can be . for the only disk, SCSI '
                      'device letter')
      self._out.Error('     or the full description listed here')
      msg = 'Found %d available disks.' % len(disks)
      if not disks:
        msg += ' Please insert an SD card and try again.'
      self._out.UserOutput(msg)

      # List available disks as a convenience.
      for disk in disks:
        self._out.UserOutput('  %s' % disk[4])

  def Em100FlashImage(self, image_fname):
    """Send an image to an attached EM100 device.

    This is a Dediprog EM100 SPI flash emulation device. We set up servo2
    to do the SPI emulation, then write the image, then boot the board.
    All going well, this is enough to get U-Boot running.

    Args:
      image_fname: Filename of image to send
    """
    args = ['spi2_vref:off', 'spi2_buf_en:off', 'spi2_buf_on_flex_en:off']
    args.append('spi_hold:on')
    self._DutControl(args)

    # TODO(sjg@chromium.org): This is for link. We could make this
    # configurable from the fdt.
    args = ['-c', 'W25Q64CV', '-d', self._tools.Filename(image_fname), '-r']
    self._out.Progress('Writing image to em100')
    self._tools.Run('em100', args, sudo=True)

    self._out.Progress('Resetting board')
    args = ['cold_reset:on', 'sleep:.2', 'cold_reset:off', 'sleep:.5']
    args.extend(['pwr_button:press', 'sleep:.2', 'pwr_button:release'])
    self._DutControl(args)


def DoWriteFirmware(output, tools, fdt, flasher, file_list, image_fname,
                    bundle, update=True, verify=False, dest=None,
                    flash_dest=None, kernel=None, bootstub=None, servo='any',
                    method='tegra'):
  """A simple function to write firmware to a device.

  This creates a WriteFirmware object and uses it to write the firmware image
  to the given destination device.

  Args:
    output: cros_output object to use.
    tools: Tools object to use.
    fdt: Fdt object to use as our device tree.
    flasher: U-Boot binary to use as the flasher.
    file_list: Dictionary containing files that we might need.
    image_fname: Filename of image to write.
    bundle: The bundle object which created the image.
    update: Use faster update algorithm rather then full device erase.
    verify: Verify the write by doing a readback and CRC.
    dest: Destination device to write firmware to (usb, sd).
    flash_dest: Destination device for flasher to program payload into.
    kernel: Kernel file to write after U-Boot
    bootstub: string, file name of the boot stub, if present
    servo: Describes the servo unit to use: none=none; any=any; otherwise
           port number of servo to use.
  """
  write = WriteFirmware(tools, fdt, output, bundle)
  write.SelectServo(servo)
  write.update = update
  write.verify = verify
  if dest == 'usb':
    method = fdt.GetString('/chromeos-config', 'flash-method', method)
    if method == 'tegra':
      tools.CheckTool('tegrarcm')
      if flash_dest:
        write.text_base = bundle.CalcTextBase('flasher ', fdt, flasher)
      elif bootstub:
        write.text_base = bundle.CalcTextBase('bootstub ', fdt, bootstub)
      ok = write.NvidiaFlashImage(flash_dest, flasher, file_list['bct'],
          image_fname, bootstub)
    elif method == 'exynos':
      tools.CheckTool('lsusb', 'usbutils')
      tools.CheckTool('smdk-usbdl', 'smdk-dltool')
      ok = write.ExynosFlashImage(flash_dest, flasher,
          file_list['exynos-bl1'], file_list['exynos-bl2'], image_fname,
          kernel)
    else:
      raise CmdError("Unknown flash method '%s'" % method)
    if ok:
      output.Progress('Image uploaded - please wait for flashing to '
          'complete')
    else:
      raise CmdError('Image upload failed - please check board connection')
  elif dest == 'em100':
    # crosbug.com/31625
    tools.CheckTool('em100')
    write.Em100FlashImage(image_fname)
  elif dest.startswith('sd'):
    write.SendToSdCard(dest[2:], flash_dest, flasher, image_fname)
  else:
    raise CmdError("Unknown destination device '%s'" % dest)
