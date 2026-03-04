# Connect Features

NoteHelper includes features designed to help you prepare for Microsoft Connect self-evaluations by surfacing customer impact data from your call logs.

## Connect Export

Generate a structured summary of your customer engagement activity over a time period. Useful for self-evaluations, manager reviews, or personal tracking.

### How to Use

1. Go to **Admin Panel** and click **Generate Export** on the Connect Export card
2. Enter a name (e.g. "H1 FY25 Connect") and select a date range
3. Click **Generate** to create a summary covering all call logs in that period

### What's Included

The export summarizes:
- **Total call logs** in the date range
- **Unique customers** engaged
- **Top topics** discussed (up to 10 displayed, sorted by frequency)
- **Per-customer breakdown** with call count, date range, and topics

### Export Formats

- **Copy to Clipboard** -- plain text format, ready to paste into Connect or an email
- **Download Markdown** -- `.md` file with formatted headings and lists
- **Download JSON** -- machine-readable format for further processing

### Previous Exports

All generated exports are saved and can be viewed or deleted from the same page. Click **View** on any previous export to reload its data.

---

## Connect Impact Signals

When importing a meeting via WorkIQ (either "Import from Meeting" or "Auto-fill"), NoteHelper can ask WorkIQ to identify **customer impact signals** from the meeting transcript. These are concrete examples of customer outcomes, adoption milestones, technical wins, or business value that you can reference in Connect self-evaluations.

### How It Works

When enabled, NoteHelper appends an extra instruction to the WorkIQ summary prompt asking it to look for impact signals. The AI identifies relevant signals and returns them in a structured format. They appear in your call log content under an **Impact Signals** heading, separated by a horizontal rule.

Example signals:
- "Customer committed to migrating 3 production workloads to AKS by Q2"
- "Reduced deployment time from 4 hours to 15 minutes using Azure DevOps pipelines"
- "Customer expanded Azure spend by $50K/month after successful POC"

If no clear impact signals are found in the meeting, the section is simply omitted.

### Toggling Impact Extraction

Impact extraction is **enabled by default**. You can control it in two places:

1. **Settings** > **WorkIQ & AI** > **Extract Connect Impact Signals** toggle
   - This sets your global default. When off, no meetings will include impact extraction unless overridden per-meeting.

2. **Per-meeting override** on the call log form
   - When importing a meeting, click **Customize summary prompt** to expand the prompt editor. The **Extract impact signals** checkbox lets you override your global setting for just that import.

### Fill My Day

The **Fill My Day** bulk import flow automatically uses your global impact extraction setting. Individual per-meeting overrides are not available in bulk mode since meetings are processed automatically.
