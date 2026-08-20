# encoding: utf-8
from __future__ import division, print_function, unicode_literals
import os
import re
from urllib.parse import unquote

import objc
from GlyphsApp import Glyphs, DOCUMENTEXPORTED
from GlyphsApp.plugins import PalettePlugin
from vanilla import Window, Group, TextBox
from Foundation import NSDate, NSMakeRect, NSTimer
from AppKit import NSViewWidthSizable, NSViewMinXMargin, NSControl, NSBezierPath, NSColor

PREF_KEY = "com.rsztype.Numeratore.enabled"
NAME_KEY = "com.rsztype.Numeratore.nameVersion"


# ----------------------------------------------------------------------
# Shared export hook, registered once for the entire app regardless of
# how many windows/palettes are open. Uses Glyphs' own documented
# callback API (Glyphs.addCallback / DOCUMENTEXPORTED) instead of a
# hand-rolled NSObject/NSNotificationCenter observer, since that's the
# supported integration point across Glyphs versions.
# ----------------------------------------------------------------------
_lastBump = 0.0
_lastExport = 0.0
_batchTag = None
_callbackRegistered = False

# One export of a family writes one file per instance and calls back once for
# each: inside this many seconds they are treated as one export.
BATCH_WINDOW = 5.0

# How the version is written on the end of a name, and how it is recognised
# there: "Nautica-v1.023.otf". Matched so that exporting twice writes one
# number rather than a queue of them.
NAME_FORMAT = "%s-v%s%s"
_TAG_AT_END = re.compile(r"[ _-]v?\d+\.\d{3}$")


def _versionTag(font):
	return "%d.%03d" % (font.versionMajor, font.versionMinor)


# What the notification calls the thing we are after, when it hands over a
# dictionary — which is what Glyphs does: fontFilePath for the file this
# export wrote, fontFilePaths for every file of the batch.
PATH_KEYS = ("fontFilePath", "fontFilePaths", "path", "filePath", "fileURL", "URL")


def _expand(value, depth=0):
	"""
	A value and everything inside it, flattened.

	The path arrives wrapped: a notification holding a dictionary holding a
	list holding the string. Rather than know which wrapping this version of
	Glyphs uses, every container is opened and everything that falls out is
	offered to _asPath — the named keys first, since a dictionary that has one
	is telling us where to look.
	"""
	if value is None or depth > 3:
		return []
	found = [value]
	try:
		keys = list(value.keys())
	except Exception:
		keys = None
	if keys is not None:
		for key in PATH_KEYS:
			if key in keys:
				found.extend(_expand(value[key], depth + 1))
		for key in keys:
			if key not in PATH_KEYS:
				found.extend(_expand(value[key], depth + 1))
		return found
	if not isinstance(value, (str, bytes)):
		try:
			items = list(value)
		except Exception:
			items = None
		if items is not None and items != [value]:
			for item in items:
				found.extend(_expand(item, depth + 1))
	return found


def _candidates(info):
	"""Everything the callback might be carrying the path in."""
	found = []
	try:
		found.extend(_expand(info.object()))
	except Exception:
		pass
	for reader in ("userInfo",):
		try:
			value = getattr(info, reader)
			found.extend(_expand(value() if callable(value) else value))
		except Exception:
			pass
	found.append(info)
	return found


def _asPath(candidate):
	"""A candidate read as a file path, whether it arrived as a string or a URL."""
	for reader in (None, "path", "fileSystemRepresentation"):
		value = candidate
		if reader is not None:
			try:
				value = getattr(candidate, reader)
				value = value() if callable(value) else value
			except Exception:
				continue
		if value is None:
			continue
		try:
			path = value.decode("utf-8") if isinstance(value, bytes) else str(value)
		except Exception:
			continue
		if path.startswith("file://"):
			path = unquote(path[7:])
		if path and os.path.isfile(path):
			return path
	return None


def _exportedFile(info):
	"""
	The file Glyphs has just written, out of whatever the callback was handed.

	The notification carries the path as its object, but the shape of these
	callbacks has changed between versions before — a string, a URL, a
	dictionary with one inside — so each of them is tried, and when none of
	them is a file that exists it says so rather than doing nothing quietly.
	"""
	for candidate in _candidates(info):
		path = _asPath(candidate)
		if path:
			return path
	print("Numeratore: the export callback carried no file path — %s"
		% ", ".join(sorted(set("%s(%r)" % (type(c).__name__, c)[:120]
			for c in _candidates(info) if c is not None))))
	return None


def _renameWithVersion(path, tag):
	"""
	Put the version on the end of the file's name: Nautica.otf → Nautica-v1.023.otf.

	The number is the one the .glyphs file is carrying, which is the number
	inside the font that was just written — the increase, when it is switched
	on, happens after the export and belongs to the next one. Every instance of
	a batch is named with the same number, for the same reason.
	"""
	folder, name = os.path.split(path)
	stem, extension = os.path.splitext(name)
	stem = _TAG_AT_END.sub("", stem)
	target = os.path.join(folder, NAME_FORMAT % (stem, tag, extension))
	if os.path.abspath(target) == os.path.abspath(path):
		return path
	os.replace(path, target)     # the same version exported twice replaces itself
	return target


def _documentExported(info):
	global _lastBump, _lastExport, _batchTag
	try:
		font = Glyphs.font
		if font is None:
			return

		# The first file of a batch opens it: the version is read there, before
		# any increase, and every file of that batch is named with it.
		now = NSDate.date().timeIntervalSince1970()
		if _batchTag is None or now - _lastExport > BATCH_WINDOW:
			_batchTag = _versionTag(font)
		_lastExport = now

		if Glyphs.defaults[NAME_KEY]:
			path = _exportedFile(info)
			if path:
				try:
					print("Numeratore: %s → %s" % (os.path.basename(path),
						os.path.basename(_renameWithVersion(path, _batchTag))))
				except OSError as error:
					print("Numeratore: could not rename %s — %s. Exporting into "
						"a folder that needs an administrator, such as "
						"/Library/Fonts, is the usual reason."
						% (os.path.basename(path), error.strerror or error))
				except Exception:
					import traceback
					print("Numeratore: could not rename %s\n%s"
						% (path, traceback.format_exc()))

		if not Glyphs.defaults[PREF_KEY]:      # switch OFF -> does nothing
			return
		if now - _lastBump < BATCH_WINDOW:     # one increase per export, not per file
			return
		_lastBump = now

		font.versionMinor += 1
		if font.versionMinor > 999:            # rollover 1.999 -> 2.000
			font.versionMajor += 1
			font.versionMinor = 0

		if font.parent:                        # updates Info panel and saves
			font.parent.saveDocument_(None)

		Glyphs.showNotification(
			"Numeratore",
			"Version \u2192 %d.%03d" % (font.versionMajor, font.versionMinor)
		)
	except Exception:
		import traceback
		print("Numeratore error: %s" % traceback.format_exc())


def _ensure_engine():
	global _callbackRegistered
	if not _callbackRegistered:
		Glyphs.addCallback(_documentExported, DOCUMENTEXPORTED)
		_callbackRegistered = True


# ----------------------------------------------------------------------
# Custom-drawn pill switch: a plain NSSwitch can't be reshaped (its knob
# is drawn entirely by the system), so this hand-draws a round track and
# a circular knob instead. Subclasses NSControl (not NSView) to get
# target/action dispatch and .state() for free, matching NSSwitch's API
# closely enough that toggle_() below needs no changes.
# ----------------------------------------------------------------------
class _RSZPillSwitch(NSControl):

	def initWithFrame_(self, frame):
		self = objc.super(_RSZPillSwitch, self).initWithFrame_(frame)
		if self is None:
			return None
		self._on = False
		self._position = 0.0     # 0 = off, 1 = on; animates between the two
		self._animTarget = 0.0
		self._timer = None
		return self

	@objc.python_method
	def setOn_(self, on):
		self._on = bool(on)
		self._position = 1.0 if self._on else 0.0   # jump, no animation for the initial state
		self.setNeedsDisplay_(True)

	def state(self):
		return 1 if self._on else 0

	def drawRect_(self, rect):
		bounds = self.bounds()
		h = bounds.size.height
		track = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, h / 2.0, h / 2.0)
		(NSColor.controlAccentColor() if self._position >= 0.5 else NSColor.quaternaryLabelColor()).set()
		track.fill()

		d = h - 4
		x = 2 + (bounds.size.width - d - 4) * self._position
		knob = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, 2, d, d))
		NSColor.whiteColor().set()
		knob.fill()

	def mouseDown_(self, event):
		self._on = not self._on
		self._animTarget = 1.0 if self._on else 0.0
		if self._timer is None:
			self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
				1.0 / 60.0, self, "_tick:", None, True)
		self.sendAction_to_(self.action(), self.target())

	def _tick_(self, timer):
		diff = self._animTarget - self._position
		if abs(diff) < 0.01:
			self._position = self._animTarget
			timer.invalidate()
			self._timer = None
		else:
			self._position += diff * 0.35            # ease toward the target each frame
		self.setNeedsDisplay_(True)


# ----------------------------------------------------------------------
# Palette: the switch in the right-hand inspector column.
# ----------------------------------------------------------------------
class NumeratorePalette(PalettePlugin):

	@objc.python_method
	def settings(self):
		self.name = "🔢 Numeratore"

		width = 160
		rowHeight = 26
		height = rowHeight * 2 + 6
		switch_width = 40
		self.paletteView = Window((width, height))
		self.paletteView.group = Group((0, 0, width, height))
		group = self.paletteView.group
		labelWidth = width - 16 - switch_width

		# Two switches, two things they do: the first counts the version up
		# after every export, the second writes that same number into the name
		# of the file the export just made. Either one is useful without the
		# other, so neither waits on the other.
		group.label = TextBox((8, 5, labelWidth, 18), "Increase Vers.", sizeStyle="small")
		group.nameLabel = TextBox((8, 5 + rowHeight, labelWidth, 18), "Vers. in name", sizeStyle="small")

		groupView = group.getNSView()
		groupView.setAutoresizingMask_(NSViewWidthSizable)

		# custom pill switches, built and attached defensively: if anything
		# about raw AppKit interop fails here, the rest of the palette (and
		# the export hook) should still come up rather than taking Glyphs down.
		self.switch = self.addSwitch(groupView, PREF_KEY, "toggle:",
			width, switch_width, 3)
		self.nameSwitch = self.addSwitch(groupView, NAME_KEY, "toggleName:",
			width, switch_width, 3 + rowHeight)

		self.dialog = groupView

	@objc.python_method
	def addSwitch(self, groupView, key, action, width, switch_width, top):
		try:
			control = _RSZPillSwitch.alloc().initWithFrame_(
				NSMakeRect(0, 0, switch_width - 8, 14))
			control.setOn_(bool(Glyphs.defaults[key]))
			frame = control.frame()
			# these views are not flipped: a row measured from the top of the
			# palette is subtracted from its height
			control.setFrameOrigin_((width - 8 - frame.size.width,
				groupView.frame().size.height - top - frame.size.height - 4))
			control.setTarget_(self)
			control.setAction_(action)
			control.setAutoresizingMask_(NSViewMinXMargin)
			groupView.addSubview_(control)
			return control
		except Exception:
			import traceback
			print("Numeratore: could not create the switch control: %s"
				% traceback.format_exc())
			return None

	@objc.python_method
	def start(self):
		_ensure_engine()   # registers the observer only once

	def toggle_(self, sender):
		Glyphs.defaults[PREF_KEY] = bool(sender.state())

	def toggleName_(self, sender):
		Glyphs.defaults[NAME_KEY] = bool(sender.state())

	@objc.python_method
	def __file__(self):
		"""Please leave this method unchanged"""
		return __file__

	# Compatibility fix: Glyphs calls these methods on palettes.
	_sortID = 0

	@objc.python_method
	def setSortID_(self, sortID):
		try:
			self._sortID = sortID
		except Exception as e:
			self.logToConsole("setSortID_: %s" % str(e))

	@objc.python_method
	def sortID(self):
		return self._sortID
