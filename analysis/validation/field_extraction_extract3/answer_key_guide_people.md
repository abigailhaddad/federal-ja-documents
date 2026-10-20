# Answer-key guide: people named in a J&A document

You are building an answer key by reading ONE document's full text. List every
person the document identifies by name, plus every signature/approval block
whose name has been visibly redacted. Read every page of every attachment;
people are often on the last pages (signature blocks) and in the first lines
(points of contact).

The text file has `##### ATTACHMENT ... #####` and `----- page N [native|transcription] -----`
markers. Those markers are NOT document text. "[transcription]" pages were
machine-transcribed from a scan: they mark redactions as `[REDACTED]`,
signatures as `[signature]`, unreadable text as `[illegible]`.

## Who to include

- Everyone identified by a personal name anywhere in the document: signers,
  approvers, certifiers, reviewers, people in digital-signature stamps,
  "Prepared by" lines, points of contact, contracting officers named in the
  narrative, vendor or other-firm employees (e.g. a company point of contact,
  a sales representative named in market research), and anyone else named
  (e.g. a public official mentioned in the narrative).
- Do NOT include: a name that is only part of an organisation, place, building,
  street, ship, product or law ("John J. Kingman Road", "Walter Reed National
  Military Medical Center", "Johnson Controls"); a person who appears ONLY
  inside an email address ("julia.r.woltjen.civ@army.mil") or a file name.
- ONE entry per person. The same person appearing as a typed name, a
  digital-signature stamp and a "Prepared by" line is one entry.

## Fields per entry

- `name`: the person's name exactly as written (keep capitalisation, initials,
  rank prefixes like "MAJ" or "Mr." only if attached to the name in the text,
  suffixes like "-S"). If the person appears in several forms, use the typed or
  printed form from the signature block or the text; use the
  digital-signature stamp form ("LASTNAME.FIRSTNAME.M.1234567890", with the
  number, exactly as written) only if it is the only form. If a name is broken
  across a line break, join the parts with a single space (or with nothing if a
  single word was split mid-word, dropping a line-end hyphen). `null` when the
  name is redacted.
- `name_variants`: EVERY form in which this person's name is written in the
  document (each copied as written, line breaks joined as above), including
  stamp forms with and without the trailing number if both occur. Empty list
  when redacted.
- `title`: the person's title or position as printed next to them
  ("Contracting Officer", "Associate Counsel, NAVWAR 3.1"), or null.
- `organization`: the organisation printed with the person (office, command,
  company), or null.
- `role_in_document`: one of
  - `contracting_officer`: a contracting officer, or contracting-office staff
    acting for it (contract specialist, buyer, procuring contracting officer),
    including a CO whose signature also serves as approval.
  - `competition_advocate`: a competition advocate / advocate for competition
    at any level.
  - `legal_reviewer`: counsel or anyone signing a legal-sufficiency review.
  - `requiring_or_technical_official`: requiring-activity or technical people:
    program/project managers, CORs, technical or requirements certifiers, end
    users, chiefs of the requiring service, engineers.
  - `signer_or_approver`: an approving official or other signer whose role is
    none of the above (head of the contracting activity, senior procurement
    executive, PEO approving, chief of the contracting office approving, etc.).
  - `vendor_employee`: an employee or representative of the proposed vendor or
    of any other firm.
  - `other`: anyone else (e.g. a public official mentioned in passing, a
    government point of contact whose role is not stated).
  If more than one fits, take the first in this order: competition_advocate,
  legal_reviewer, contracting_officer, requiring_or_technical_official,
  vendor_employee, signer_or_approver, other.
- `redacted`: true ONLY when the person's NAME is visibly withheld: a
  `[REDACTED]` marker, "(b)(6)", "[REDACTED (b)(6)]", the word "redacted", or
  a blacked-out marker in the place where a signature block's name would be.
  Then `name` is null and you record the title/organization/role as printed.
  One entry per redacted block (three redacted signature blocks = three
  entries).
  - NOT redacted: a name that is visible even though its phone number or the
    number in a digital-signature stamp is redacted ("COLE.WADE.CLAY.[REDACTED]"
    is the visible name COLE.WADE.CLAY). Record the person, redacted false.
  - NO ENTRY at all: a blank, unsigned block (a printed title with an empty
    name line or underscores and no redaction marker), or "[signature]" with no
    name and no redaction marker. These are not named people and not visible
    redactions.
- `in_signature_block`: true if the person (or redacted block) is in a
  signature, certification, approval, review or "prepared by" block, or a
  digital-signature stamp; false otherwise (points of contact, narrative,
  vendor staff in market research).
- `source_passage`: a short passage (5-30 words) copied from the text that
  contains the name (or, for a redacted entry, the redaction marker and the
  title).
- `page`: "<attachment resource id>#<page number>" where the passage is.
- `notes`: anything a reviewer needs (e.g. "same person also signs as stamp
  HOWE.KEVIN.ALLEN", "unclear whether [REDACTED] covers the name or only the
  signature").

Never guess a name, never use outside knowledge, never fill in a redaction.

## Output

Write one JSON object per document to the file you are told, exactly:

```json
{"notice_id": "...", "people": [ {"name": ..., "name_variants": [...], "title": ..., "organization": ..., "role_in_document": "...", "redacted": false, "in_signature_block": true, "source_passage": "...", "page": "...", "notes": null} ], "doc_notes": null}
```

`doc_notes`: anything unusual about the document (garbled text, unsigned
template, a block you could not decide). Before writing, re-scan the text for
every capitalised two-word sequence, "Mr.", "Ms.", "Dr.", "Digitally signed",
"Prepared by", "Point of Contact", "/s/", "[REDACTED", "(b)(6)", "Signature",
"Name:" to make sure you missed no one.
