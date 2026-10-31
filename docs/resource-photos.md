# Supply and Request photos

Both resources support one optional photo on create and edit. List/detail
responses include `image`, a URL or null. Existing JSON clients remain valid.

POST/PATCH accepts either the existing JSON body or multipart/form-data with:

- `payload`: the complete JSON object, preserving booleans, coordinate numbers,
  nested location and suitability arrays.
- `image`: one JPG, PNG or WebP file, up to 5 MiB and 20 megapixels.

To remove a photo, PATCH JSON `{ "image": null }`. Omitting `image` preserves
the existing photo. A multipart photo can be changed without other field edits.
The existing owner/admin mutation permissions and CSRF protection apply.

The backend decodes the image, applies EXIF orientation, strips metadata and
re-encodes a JPEG (maximum dimensions 1920×1920). Unsupported/corrupt images
return 400 with an `image` field error. Filenames are random UUIDs.

`GET /api/v1/media/photos/<uuid>.jpg` serves the stored file through Django,
including when DEBUG=False. Its permissions follow the parent resource: public
while publicly visible, otherwise owner/admin/reservation participants. Removed
or replaced image URLs no longer resolve through the API. Responses use
`private, no-store` and `nosniff`.

Install requirements and run `python manage.py migrate` before serving. Set
`MEDIA_ROOT` to a persistent writable directory on the backend host; the default
is `<repo>/media`. Ephemeral deployments must provide persistent storage before
uploading real photos. Replaced/deleted files are not currently garbage-collected
from storage; they cannot be fetched through the API after losing their parent
reference.
