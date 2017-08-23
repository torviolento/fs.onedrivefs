#!/usr/bin/env python3

from io import BytesIO, RawIOBase, UnsupportedOperation
from itertools import chain
from os import close, remove, write
from tempfile import mkstemp

from fs.base import FS
from fs.errors import DestinationExists, DirectoryExpected, FileExpected, ResourceNotFound, ResourceReadOnly
from fs.info import Info
from fs.mode import Mode
from fs.path import basename, dirname
from fs.subfs import SubFS
from fs.time import datetime_to_epoch, epoch_to_datetime
from onedrivesdk import AuthProvider, FileSystemInfo, Folder, HttpProvider, Item, OneDriveClient
from onedrivesdk.error import OneDriveError
from temp_utils.contextmanagers import temp_file

class TempFileWrapper(IOBase):
	def __init__(self, client, path):
		super().__init__()
		self.client = client
		self.uploadPath = path
		self.fileHandle, self.localPath = mkstemp(prefix="pyfilesystem-onedrive-", text=False)

	def write(self, b):
		write(self.fileHandle, b)

	def readinto(self, b):
		# we're write-only
		raise UnsupportedOperation()

	def close(self):
		close(self.fileHandle)
		# upload to OneDrive
		item = self.client.item(path=self.uploadPath).upload(self.localPath)
		remove(self.localPath)
		super().close()

class OneDriveFS(FS):
	def __init__(self, clientId, sessionType):
		super().__init__()
		httpProvider = HttpProvider()
		authProvider = AuthProvider(http_provider=httpProvider, client_id=clientId, scopes=["wl.signin", "wl.offline_access", "onedrive.readwrite"], session_type=sessionType)
		self.client = OneDriveClient("https://api.onedrive.com/v1.0/", authProvider, httpProvider)
		self.client.auth_provider.load_session()

		_meta = self._meta = {
			"case_insensitive": False, # I think?
			"invalid_path_chars": ":", # not sure what else
			"max_path_length": None, # don't know what the limit is
			"max_sys_path_length": None, # there's no syspath
			"network": True,
			"read_only": False, # at least until openbin is fully implemented
			"supports_rename": False # since we don't have a syspath...
		}

	def __repr__(self):
		return f"<OneDriveFS>"

	def _itemInfo(self, item): # pylint: disable=no-self-use
		# Looks like the dates returned directly in item.file_system_info (i.e. not file_system_info) are UTC naive-datetimes
		# We're supposed to return timestamps, which the framework can convert to UTC aware-datetimes
		rawInfo = {
			"basic": {
				"name": item.name,
				"is_dir": item.folder is not None,
			},
			"details": {
				"accessed": None, # not supported by OneDrive
				"created": datetime_to_epoch(item.file_system_info.created_date_time),
				"metadata_changed": None, # not supported by OneDrive
				"modified": datetime_to_epoch(item.file_system_info.last_modified_date_time),
				"size": item.size,
				"type": 1 if item.folder is not None else 0,
			}
		}
		if item.photo is not None:
			rawInfo.update({"photo":
				{
					"camera_make": item.photo.camera_make,
					"camera_model": item.photo.camera_model,
					"exposure_denominator": item.photo.exposure_denominator,
					"exposure_numerator": item.photo.exposure_numerator,
					"focal_length": item.photo.focal_length,
					"f_number": item.photo.f_number,
					"taken_date_time": item.photo.taken_date_time,
					"iso": item.photo.iso
				}})
		if item.location is not None:
			rawInfo.update({"location":
				{
					"altitude": item.location.altitude,
					"latitude": item.location.latitude,
					"longitude": item.location.longitude
				}})
		if item.tags is not None:
			rawInfo.update({"tags":
				{
					"tags": list(item.tags.tags)
				}})
		return Info(rawInfo)

	def getinfo(self, path, namespaces=None):
		try:
			item = self.client.item(path=path).get()
		except OneDriveError as e:
			raise ResourceNotFound(path=path, exc=e)
		return self._itemInfo(item)

	def setinfo(self, path, info): # pylint: disable=too-many-branches
		itemRequest = self.client.item(path=path)
		try:
			existingItem = itemRequest.get()
		except OneDriveError as e:
			raise ResourceNotFound(path=path, exc=e)

		itemUpdate = Item()
		itemUpdate.id = existingItem.id
		itemUpdate.file_system_info = FileSystemInfo()

		for namespace in info:
			for name, value in info[namespace].items():
				if namespace == "basic":
					if name == "name":
						assert False, "Unexpected to try and change the name this way"
					elif name == "is_dir":
						# can't change this - must be an error in the framework
						assert False, "Can't change an item to and from directory"
					else:
						assert False, "Aren't we guaranteed that this is all there is in the basic namespace?"
				elif namespace == "details":
					if name == "accessed":
						pass # not supported by OneDrive
					elif name == "created":
						# incoming datetimes should be utc timestamps, OneDrive expects naive UTC datetimes
						itemUpdate.file_system_info.created_date_time = epoch_to_datetime(value).replace(tzinfo=None)
					elif name == "metadata_changed":
						pass # not supported by OneDrive
					elif name == "modified":
						# incoming datetimes should be utc timestamps, OneDrive expects naive UTC datetimes
						itemUpdate.file_system_info.last_modified_date_time = epoch_to_datetime(value).replace(tzinfo=None)
					elif name == "size":
						assert False, "Can't change item size"
					elif name == "type":
						assert False, "Can't change an item to and from directory"
					else:
						assert False, "Aren't we guaranteed that this is all there is in the details namespace?"
				else:
					# ignore namespaces that we don't recognize
					pass
		itemRequest.update(itemUpdate)

	def listdir(self, path):
		return [x.name for x in self.scandir(path)]

	def makedir(self, path, permissions=None, recreate=False):
		parentDir = dirname(path)
		itemRequest = self.client.item(path=parentDir)
		try:
			item = itemRequest.get()
		except OneDriveError as e:
			raise ResourceNotFound(path=parentDir, exc=e)

		if item.folder is None:
			raise DirectoryExpected(path=parentDir)
		newItem = Item()
		newItem.name = basename(path)
		newItem.folder = Folder()
		itemRequest.children.add(entity=newItem)
		# don't need to close this filesystem so we return the non-closing version
		return SubFS(self, path)

	def openbin(self, path, mode="r", buffering=-1, **options):
		mode = Mode(mode)
		if mode.exclusive and self.exists(path):
			raise FileExists(path=path)
		if self.exists(path) and not self.isfile(path):
			raise FileExpected(path=path)
		itemRequest = self.client.item(path=path)
		if mode.reading:
			try:
				_ = itemRequest.get()
			except OneDriveError as e:
				raise ResourceNotFound(path=path, exc=e)
			with temp_file() as localPath:
				existingData = itemRequest.download(localPath)
				with open(localPath, "rb") as f:
					existingData = f.read()
			return BytesIO(existingData)
		elif mode.writing:
			return TempFileWrapper(client=self.client, path=path)
		elif mode.appending:
			raise ResourceReadOnly(path=path)
		else:
			raise ResourceReadOnly(path=path)

	def remove(self, path):
		itemRequest = self.client.item(path=path)
		if itemRequest.get().folder is not None:
			raise FileExpected(path=path)
		itemRequest.delete()

	def removedir(self, path):
		itemRequest = self.client.item(path=path)
		if itemRequest.get().folder is None:
			raise DirectoryExpected(path=path)
		itemRequest.delete()

	# non-essential method - for speeding up walk
	def scandir(self, path, namespaces=None, page=None):
		itemRequest = self.client.item(path=path)
		try:
			item = itemRequest.get()
		except OneDriveError as e:
			raise ResourceNotFound(path=path, exc=e)
		if item.folder is None:
			raise DirectoryExpected(path=path)

		childrenRequest = itemRequest.children.request()
		children = childrenRequest.get()
		result = (self._itemInfo(x) for x in children)

		while hasattr(children, "_next_page_link"):
			childrenRequest = childrenRequest.get_next_page_request(children, self.client)
			children = childrenRequest.get()
			result = chain(result, (self._itemInfo(x) for x in children))

		return result
