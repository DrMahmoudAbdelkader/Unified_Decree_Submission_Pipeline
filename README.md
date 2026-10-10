# Failed merged PDFs

Merged submission PDFs (MDT form + medical report + patient document, already
signed and compressed) for cases that FAILED to submit, kept so they can be
submitted by hand.

- `failed_merged_pdfs/<date>/case<id>__<national_id>_<mdt_id>.pdf` - the file
- `failed_merged_pdfs/<date>/case<id>__<national_id>_<mdt_id>.txt` - why it failed

Folders older than 10 days are removed automatically. This branch is rebuilt
as a single commit on every run; do not expect history here.

Contains patient data - keep the repository private.
