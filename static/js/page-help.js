/**
 * Contextual page help — press F1 for help about the current page.
 *
 * Maps URL patterns to help content (title, description, tips).
 * Falls back to a generic help message for unmapped pages.
 */
const PageHelp = (function () {

  // ── Help content by route pattern ───────────────────────────────────
  // Patterns are checked top-to-bottom; first match wins.
  // Use {id} as a placeholder for numeric path segments.

  const helpMap = [
    { pattern: /^\/reports\/sql-(renewals|eos)$/, title: 'SQL Motions', content: `
      <p>SQL Renewals and SQL 2016 EOS / ESU share one SQL Motions sync.
      Both source reports must succeed before the saved snapshot changes.</p>
      <p><strong>SQL Server 2016 data is known to be incorrect.</strong>
      Do not rely on its counts or customer lists. The report, widgets and exports retain
      this warning. This data-quality warning does not apply to SQL Renewals.</p>
      <ul>
        <li>Renewals includes every future SQL expiration, nearest first, including
        zero and unknown amounts. The 90-day count is a summary, not a queue cutoff.</li>
        <li>SQL 2016 candidates require explicit version evidence and positive
        All Discovered counts. Overlapping license categories are not added again.</li>
        <li>ESU protection and discovery units are unverified. Missing source data
        means unknown. The EOS view focuses only on version discovery and subscriptions.</li>
        <li>Dashboard click-through preserves customer scope. Group by seller
        changes presentation only. CSV, clipboard and email export the visible queue.</li>
        <li>SQL license cores are on-prem licenses with SA. Modeled monthly ACR
        is an estimate, not qualified pipeline or a forecast.</li>
      </ul>` },
    { pattern: /^\/templates\/(?:new|\d+\/edit)$/, title: 'Note Template Form', content: `
      <p>Create or edit reusable rich-text content for notes. Template name and
      content are required. Nothing saves until Create Template or Update Template.</p>
      <ul>
        <li>Use headings, emphasis, lists, indentation, links and Clear formatting.
        The formatting toolbar and manual commands stay visible while content scrolls.</li>
        <li>Back to Settings and Cancel return to Settings. Unsaved changes are
        guarded; browser Back restores this entry's draft and reading position.</li>
        <li>Failed saves retain entries for correction and retry. Settings keeps
        customer/general defaults separate; inserting a template into a note still
        offers Replace or Add to top when that note already has content.</li>
      </ul>` },
    { pattern: /^\/project\/\d+$/, title: 'Project Details', content: `
      <p>Read internal work, its description and dates, action items and linked notes.
      Edit, Delete and Back remain visible while the dossier scrolls.</p>
      <ul>
        <li>Add an action item with a title and optional due date. It saves only
        when you submit Add; failed saves keep your input for retry.</li>
        <li>Open a task for its rich description, source and related note, then
        edit or complete it using the existing task controls.</li>
        <li>Linked notes remain newest first. New note links to this project.</li>
        <li>Back returns to your reading context. Unsubmitted task changes and
        pending saves are protected, including Initiative Tracker's nested dialogs.</li>
      </ul>` },
    { pattern: /^\/project\/(?:new|\d+\/edit)$/, title: 'Project Form', content: `
      <p>Track internal work, learning and achievements. Title is required;
      description, type and due date retain their original optional behavior.
      Description is plain text, not a rich-text editor.</p>
      <ul>
        <li>Save and Cancel stay visible while the fields and general notes scroll.
        Cancel returns to Engagements for a new project, or to the project when editing.</li>
        <li>Status is available only when editing. General notes appear newest first;
        notes already attached to this project remain selected.</li>
        <li>There is no autosave. Leaving with changes opens a discard guard;
        Back restores this entry's fields, selections and reading position.</li>
        <li>Failed saves keep your entries visible for correction and retry.</li>
      </ul>
    `},
    { pattern: /^\/(?:customer\/\d+\/engagement\/new|engagement\/\d+\/edit)$/, title: 'Engagement Form', content: `
      <p>Write the engagement story alongside this customer's existing notes,
      opportunities and milestones. Title is required; the other fields remain optional.</p>
      <ul>
        <li>Save and Cancel stay visible while the story and linked work scroll.
        Cancel returns to the customer, including when editing an engagement.</li>
        <li>Existing people and checked associations stay in the unsubmitted form.
        Adding a new customer or internal contact saves that person immediately.</li>
        <li>Unsaved changes are guarded when leaving. Back restores this entry's
        fields, people, selections and reading positions. There is no autosave.</li>
        <li>Validation and failed saves keep your entries visible for correction.
        Deleting an engagement removes the thread, not its linked records.</li>
      </ul>
    `},
    { pattern: /^\/action-items$/, title: 'Action Items', content: `
      <p>Local follow-ups are separate from MSX HoK tasks. Engagement, project and
      saved/generated Copilot queues retain their existing open-task scope.</p>
      <ul>
        <li>Five divider-separated dashboard cells scroll independently.
        Data headings sort both ways.</li>
        <li>Click a project task row to view or edit it; the project link opens the
        project directly. Project sort controls follow the fields shown in the summary.
        Other task queues retain View / Edit. Completion, Copilot Save, Dismiss and
        Not Useful keep their original behavior.</li>
        <li>Open the linked engagement or project for creation, rich editing,
        reopening and deletion. Source and linked-note links stay in the preview.</li>
        <li>Hygiene and stale milestones are maintenance records, not local tasks.
        Hygiene sorts by Name or Customer, not the diagnostic gap.
        The stale seller filter and each cell's reading position return with Back.</li>
      </ul>
    `},
    // ── Home ──
    { pattern: /^\/$/, title: 'Dashboard', content: `
      <p>Your home base — calendar of notes &amp; meetings, plus quick access to drafts and engagements.</p>

      <h6>The calendar</h6>
      <ul>
        <li><strong>Solid colored entries</strong> are real notes you've written. Click to view, hover to highlight all notes for that customer.</li>
        <li><strong>Dashed-border entries</strong> are <em>ghost meetings</em> — meetings pulled from your Outlook calendar that don't have a note yet. Click one to start a note pre-filled with the meeting subject and external attendees.</li>
        <li><strong>Today's badge</strong> turns <span class="sb-help-indicator">orange</span> if today's meetings haven't been refreshed today. Click it to pull a fresh copy from WorkIQ.</li>
        <li><strong>+</strong> on any day creates a new note dated to that day.</li>
        <li><strong>X</strong> on a ghost dismisses it. For recurring meetings, hover the <i class="bi bi-arrow-repeat"></i> icon and click the red X to dismiss the whole series.</li>
      </ul>

      <h6>Calendar icon legend</h6>
      <table class="sb-help-table sb-help-legend">
        <tbody>
          <tr><td><i class="bi bi-star-fill sb-help-hva"></i></td><td>High-Value Activity (HVA) attached</td></tr>
          <tr><td><i class="bi bi-check2-square sb-help-task"></i></td><td>Other MSX task attached</td></tr>
          <tr><td><i class="bi bi-flag-fill sb-help-milestone"></i></td><td>Milestone linked, with no task attached yet</td></tr>
          <tr><td><i class="bi bi-diagram-3 sb-help-engagement"></i></td><td>Engagement linked</td></tr>
          <tr><td><i class="bi bi-sticky sb-help-muted"></i></td><td>General note without a customer</td></tr>
          <tr><td><i class="bi bi-calendar-event sb-help-muted"></i></td><td>Outlook meeting matched to a customer</td></tr>
          <tr><td><i class="bi bi-question-circle sb-help-warning"></i></td><td>Outlook meeting needs a customer match</td></tr>
          <tr><td><i class="bi bi-arrow-repeat sb-help-muted"></i></td><td>Recurring meeting</td></tr>
        </tbody>
      </table>

      <h6>Background meeting sync (the typing dots)</h6>
      <p>Sales Buddy quietly pulls today + the next 4 business days of meetings from WorkIQ each morning, plus a catch-up sweep whenever you start the app and any of those days are stale. While that's running, you'll see <strong>typing dots</strong> on each calendar day:</p>
      <ul>
        <li>Dots mean that day's meetings are being fetched or queued up next</li>
        <li>As each day finishes, its ghosts fade in and the dots disappear</li>
        <li><strong>Click any dot</strong> to surface the floating progress card. Close it with the X — sync keeps running in the background.</li>
      </ul>

      <h6>Quick actions</h6>
      <ul>
        <li><span class="kbd">/</span> — jump to any customer</li>
        <li><span class="kbd">Ctrl</span>+<span class="kbd">K</span> — quick note creation</li>
        <li><span class="kbd">?</span> — full keyboard shortcuts list</li>
      </ul>
    `},

    // ── Notes ──
    { pattern: /^\/notes\/?$/, title: 'Notes', content: `
      <p>Read your notes newest first, with customer identity, dates, short excerpts
      and linked people, topics and work. Seller mode keeps its existing account scope.</p>
      <ul>
        <li>All, Customer and General filter the original note scope.
        There are no seller, topic or date-range filters on this directory.</li>
        <li>Per page remembers 25, 50, 100 or 200 locally. Changing it starts at
        page one; pagination keeps the current filter and page size.</li>
        <li>Click a date, excerpt or note row to open the full note. Relationship
        links open their own records. Back restores this page's reading position.</li>
        <li>The status rail shows the displayed range and total in the current scope.
        This reading stream is chronological, not a sortable data table.</li>
        <li>Use the shared New Note command, or Create your first note when empty,
        to choose a customer or a General Note. Nothing saves from this directory.</li>
      </ul>
    `},
    { pattern: /^\/note\/new/, title: 'New Note', content: `
      <p>Create a new call note. Capture what happened, who was there, and what technologies were discussed.</p>
      <h6>Tips</h6>
      <ul>
        <li>Select a <strong>customer</strong> first — this links the note to their record</li>
        <li>Add <strong>topics</strong> to categorize what was discussed (e.g., "Fabric", "Copilot")</li>
        <li>Use <span class="kbd">Ctrl</span>+<span class="kbd">Enter</span> to save quickly</li>
        <li>The AI can auto-suggest topics from your note content</li>
      </ul>
    `},
    { pattern: /^\/note\/\d+\/edit/, title: 'Edit Note', content: `
      <p>Update this note's content, topics, or attachments.</p>
      <h6>Tips</h6>
      <ul>
        <li>Use <span class="kbd">Ctrl</span>+<span class="kbd">Enter</span> to save quickly</li>
        <li>Changes are saved when you click <strong>Save</strong> — there's no auto-save</li>
      </ul>
    `},
    { pattern: /^\/note\/\d+/, title: 'View Note', content: `
      <p>Full details of this call note — content, topics, sellers, and linked customer.</p>
      <h6>Actions</h6>
      <ul>
        <li><strong>Edit</strong> — modify the note content or tags</li>
        <li><strong>Delete</strong> — permanently remove (cannot be undone)</li>
        <li>Click any <strong>topic badge</strong> to see all notes with that topic</li>
        <li>Click the <strong>customer name</strong> to jump to their record</li>
      </ul>
    `},

    // ── Customers ──
    { pattern: /^\/(?:revenue\/)?products\/?$/, title: 'Products', content: `
      <p>Find imported products and compare their usage and total revenue, highest revenue first.</p>
      <ul>
        <li>Search product names, usage counts or formatted revenue. Clear restores the complete directory.</li>
        <li>Click any column heading in either pane to sort ascending; click again for descending. Arrows show the current order. Sorting and browsing context survive Back.</li>
        <li>Select a product on the left to compare its customers, buckets and all-history consumption on the right.</li>
        <li>Choose a customer name on the right to open Product Revenue with that exact customer/bucket entry selected, including its graph and monthly history. The arrow beside a matched name opens Customer Hub.</li>
        <li>Open product goes directly to the complete history workbench. Browser Back restores your product, search and both reading positions.</li>
        <li>Usage entries count positive-revenue customer/bucket pairs, not unique customers. Consolidated products add counts across their members.</li>
        <li>Search only changes visible products. Values include all imported months and do not change saved bucket filters.</li>
      </ul>
    `},
    { pattern: /^\/(?:revenue\/product|product)\/.+$/, title: 'Product Revenue', content: `
      <p>Compare customer entries using one product, then inspect imported customer history alongside the table.</p>
      <ul>
        <li>Search customer names, buckets or total revenue. Select a row or its clock button to view history.</li>
        <li>Every comparison and history column sorts both ways. Fiscal months sort by their calendar dates; revenue and change sort by exact values, not rounded text. Unavailable changes stay last.</li>
        <li>Sorting history does not reorder the graph or recalculate Change, which still compares preceding imported months.</li>
        <li>Arriving from Products selects the customer/bucket entry you chose, even when several buckets share a customer name.</li>
        <li>Customer-name links open Customer Revenue; the arrow beside a matched name opens Customer Hub.</li>
        <li>Entries are customer/bucket rows, so a customer may appear more than once. Totals and averages include all entries and all imported months, not just search results.</li>
        <li>History retains the imported customer grouping. Change is unavailable when preceding revenue is zero or negative.</li>
        <li>The monthly revenue graph reads oldest to newest. Hover a point for its exact amount; missing imported months are gaps, not zero revenue. The table below retains every original value.</li>
        <li>For consolidated products, expand the member list to see every included product.</li>
      </ul>
    `},
    { pattern: /^\/customers\/?$/, title: 'Customers', content: `
      <p>Your customer directory. Each customer can have notes, engagements, opportunities, and revenue data linked to them.</p>
      <h6>Tips</h6>
      <ul>
        <li>A-Z, Grouped and By Notes retain the saved directory order. Show/Hide No Notes changes the saved note filter.</li>
        <li>Click a data heading to sort both ways within the current seller group or list. Back restores sorting, expanded groups and reading position.</li>
        <li>Manual opens customer entry. Import by TPID looks up an account only when you click Look Up; Import Customer saves the previewed account.</li>
        <li>Click a customer to see all their linked notes, engagements, and revenue</li>
        <li><span class="kbd">/</span> opens quick customer lookup from any page</li>
      </ul>
    `},
    { pattern: /^\/customer\/new/, title: 'New Customer', content: `
      <p>Add a new customer to track. You'll be able to attach notes, engagements, and revenue data to them.</p>
      <h6>Tips</h6>
      <ul>
        <li><strong>Name and TPID</strong> are required. TPID is a whole number, zero or greater, and must be unique.</li>
        <li>Adding a territory helps with filtering and reporting later</li>
        <li>Save Customer is manual. Cancel returns to Customers; leaving with unsaved entries offers a discard guard. Back restores this entry's draft.</li>
        <li>Create Seller saves a seller immediately and returns here. Use Back to recover unsaved customer entries. Create Territory opens a separate tab.</li>
      </ul>
    `},
    { pattern: /^\/customer\/\d+\/edit/, title: 'Edit Customer', content: `
      <p>Update the original name, nickname, TPID, MSX account URL, website, seller,
      territory and available primary CSAM. Name and TPID remain required.</p>
      <p>Save Customer returns to the customer; Cancel returns to Customers.
      Changes are not autosaved. Failed saves retain your entries for retry.
      Website changes keep the existing favicon lookup; clearing the website clears its favicon.</p>
    `},
    { pattern: /^\/(?:revenue\/customer\/\d+\/bucket\/[^/]+|customer\/\d+\/revenue\/bucket\/[^/]+)\/?$/, title: 'Bucket Product History', content: `
      <p>Product revenue for one customer and bucket, with analysis alongside the history.</p>
      <ul>
        <li>The recent grid shows up to seven imported months. Product totals include all imported months.</li>
        <li>Expand <strong>Full Product History</strong> to read every product's monthly history, initially newest first. Every recent-product and history column sorts ascending or descending; totals remain pinned.</li>
        <li>Change compares each imported month with the preceding imported month. It is unavailable when preceding revenue is zero or negative.</li>
        <li>Use the pencil to review the alert, a product name for its detail page, or <strong>Customer Revenue</strong> to return to bucket navigation.</li>
      </ul>
    `},
    { pattern: /^\/(?:revenue\/customer\/\d+|customer\/\d+\/revenue)\/?$/, title: 'Customer Revenue', content: `
      <p>Bucket analysis and monthly product revenue for this customer.</p>
      <ul>
        <li>Use <strong>Buckets</strong> to filter analysis and history together.</li>
        <li>Use the left context pane to examine a visible bucket without changing your saved filter.</li>
        <li><strong>Overview</strong> shows Total Revenue, which includes hidden buckets. Total columns include all imported months, not just the seven displayed.</li>
        <li>Click any Overview or product-grid column to toggle ascending/descending. Summary totals remain pinned and Buckets filters stay unchanged.</li>
        <li>Use <strong>Full History</strong> for all months in a bucket, or the pencil to review an alert.</li>
      </ul>
    `},
    { pattern: /^\/customer\/\d+/, title: 'Customer Details', content: `
      <p>Read the account dossier in Engagements, Notes, Opportunities and Revenue tabs.
      Identity, ownership and contacts remain alongside the selected work.</p>
      <h6>Actions</h6>
      <ul>
        <li>New Note, New Engagement and Edit Customer retain their original destinations.</li>
        <li>Find in pipeline searches opportunity, milestone and workload text without changing the account scope.</li>
        <li>Revenue headings sort both ways using raw values. Details opens the original full revenue history and graphs.</li>
        <li>Back restores the selected tab, pipeline search and reading position. Account context Save and contact Done are manual; leaving with unsaved entries offers a discard guard.</li>
        <li>Contact photo paste saves immediately. Auto-fill scans only when requested; saving a domain and applying selected contacts are explicit writes.</li>
      </ul>
    `},

    // ── Engagements ──
    { pattern: /^\/engagements\/?$/, title: 'Engagements', content: `
      <p>Browse the complete engagement directory in your existing seller or SE scope.</p>
      <p>Status, Favorites only and Group by seller/customer retain their saved selections. Last Updated uses the provider's exact update order with favorites first; story and target ordering work both ways. Table headings sort raw values within the original customer groups, or across the flat list.</p>
      <p>New Engagement opens the customer picker. Engagement and customer links keep their original destinations; Internal Projects keep their separate scope and New Project action. Back restores filters, header sorting and reading position. Favorite saves must finish before leaving.</p>
    `},
    { pattern: /^\/engagement\/\d+\/edit/, title: 'Edit Engagement', content: `
      <p>Update this engagement's details, status, and linked milestones.</p>
    `},
    { pattern: /^\/engagement\/\d+/, title: 'Engagement Details', content: `
      <p>Read the engagement story and relationships alongside its follow-ups and linked notes.
      Each side scrolls independently; Back restores reading position and completed-task disclosure.</p>
      <h6>Actions</h6>
      <ul>
        <li><strong>Generate Story</strong> — AI creates a summary from linked call notes</li>
        <li><strong>Link Notes</strong> — attach relevant call notes to this engagement</li>
        <li><strong>Milestones</strong> — track this engagement against MSX milestones</li>
        <li><strong>Action items</strong> keep rich descriptions, contacts, priority and due dates.
        Add, edit, completion, reopening, deletion and drag ordering retain their existing APIs.</li>
        <li>Unsaved editors and pending saves protect navigation. Failed writes retain entries
        and show an error so you can retry. The inline Action Items view uses the same complete body.</li>
      </ul>
    `},

    // ── Sellers ──
    { pattern: /^\/sellers\/?$/, title: 'Sellers', content: `
      <p>Your team directory. Sellers are the people who attend customer calls and own relationships.</p>
      <h6>Tips</h6>
      <ul>
        <li>Click a seller to see all their linked notes and customers</li>
        <li>Sellers can be assigned to territories for filtering</li>
      </ul>
    `},
    { pattern: /^\/seller\/new/, title: 'New Seller', content: `
      <p>Add a new seller to your team. They'll appear as an option when creating notes.</p>
    `},
    { pattern: /^\/seller\/\d+\/?$/, title: 'Seller Details', content: `
      <p>Customer coverage stays beside Milestones, Revenue and Engagements.
      Customer and latest-note links retain their original destinations.</p>
      <ul>
        <li>Compact Milestones shows uncommitted, on-team records and a separate stale list. Full View opens the complete seller-locked tracker with its original filters and exports.</li>
        <li>Revenue keeps your existing bucket selection. An alert opens its complete product history and review workflow; Full View keeps the broader alert list underneath.</li>
        <li>Engagements includes Active and On Hold records only, with status, grouping, order and Favorites only controls. Favorite changes save immediately.</li>
        <li>Data headings sort raw values both ways. Back restores the selected workstream, sorting and independent reading positions.</li>
        <li>1:1 Notes opens the persistent workspace, creating it if needed. View as Seller changes your active role context. Delete is available only without customers and requires confirmation.</li>
      </ul>
    `},

    // ── Territories ──
    { pattern: /^\/territory\/(?:new|\d+\/edit)$/, title: 'Territory Form', content: `
      <p>Name the region or segment used to organize coverage. Territory Name is
      required; existing territories appear alphabetically to help avoid duplicates.</p>
      <ul>
        <li>Save Territory and Cancel stay visible while the form scrolls.
        Cancel returns to Territories in both New and Edit.</li>
        <li>There is no autosave. Leaving with changes offers a discard guard;
        Back restores this entry's name and reading position.</li>
        <li>Validation and failed saves retain your entries for correction and retry.
        Creating a duplicate opens the existing territory; editing a duplicate
        keeps you on the editor.</li>
        <li>Renaming a territory does not change its POD, sellers, customers or
        solution engineers.</li>
      </ul>` },
    { pattern: /^\/territories\/?$/, title: 'Territories', content: `
      <p>Organize your customers and sellers by territory (region, segment, etc.).</p>
      <h6>Tips</h6>
      <ul>
        <li>Sort Name, POD, Sellers or Customers in either direction. Back restores sorting and reading position.</li>
        <li>Open a territory from its name or row; POD and seller links open their own details.</li>
        <li>Customer counts use direct territory assignments. New Customer preselects that row's territory.</li>
        <li>Territories help filter and group across the app</li>
        <li>Assign territories to customers and sellers for better organization</li>
      </ul>
    `},
    { pattern: /^\/territory\/\d+/, title: 'Territory Details', content: `
      <p>Read the territory's seller roster beside Accounts or Recent Notes.</p>
      <ul>
        <li>Seller customer counts include each seller's full coverage. Accounts
        include customers owned by Growth and Acquisition sellers assigned to
        this territory, even when a customer's direct territory differs.</li>
        <li>Recent Notes use direct customer territory and the last seven days.
        Account note counts include all dates. Neither view uses personal seller filters.</li>
        <li>Changing Recent Notes / Accounts explicitly saves your view preference
        and reloads the page. A failed save keeps the current view and offers retry.</li>
        <li>Sort Name, Customers, Notes or Note date in either direction.
        Back restores sorting and each reading pane without changing the saved view.</li>
        <li>POD, seller, account and note links open their original details.
        Edit Territory opens the manual naming form without changing coverage relationships.</li>
      </ul>
    `},

    // ── Topics ──
    { pattern: /^\/topics\/?$/, title: 'Topics', content: `
      <p>Browse all technologies and themes tagged on notes, with their descriptions
      and total note counts. Topic scope does not change with your seller role.</p>
      <ul>
        <li>Open a name or row for its original topic details and associated notes.
        New Topic opens the existing manual creation form.</li>
        <li>Sort Name, Description or Notes in either direction. These header sorts
        are temporary; Back restores your sorting and reading position.</li>
        <li>Default explicitly saves alphabetical or most-notes ordering and reloads.
        This resets the temporary sort. A failed save keeps the current order for retry.</li>
        <li>Descriptions retain their original compact preview; hover for full text.
        The AI can still suggest topics when you create a note.</li>
      </ul>
    `},
    { pattern: /^\/topic\/(?:new|\d+\/edit)\/?$/, title: 'Topic Form', content: `
      <p>Name is required; description is optional. Save Topic manually saves both fields.
      Nothing is saved while typing.</p>
      <ul>
        <li>Cancel returns to Topics. Unsaved navigation offers Keep editing or Discard changes.</li>
        <li>Back restores this entry's draft and reading position. Failed saves retain your entries
        for review and explicit retry.</li>
        <li>New duplicates open the existing topic; Edit duplicates keep your entries in the form.</li>
      </ul>
    `},
    { pattern: /^\/topic\/\d+\/?$/, title: 'Topic Details', content: `
      <p>All notes tagged with this topic, across all dates and seller roles.
      The full description, total tagged-note count and unique account count remain available.</p>
      <ul>
        <li>Group by account starts collapsed. Sort the account table by Customer, Seller,
        Territory, Latest or Number of notes in either direction. Latest shows the date
        of the account's newest note tagged with this topic and defaults to
        newest first, using the full note time to order notes on the same date.
        Sort Latest descending to return to that order.
        Use the arrow beside an account
        to expand its notes; multiple accounts can stay open and travel with their account
        when sorted, without changing the notes' individual sorting. No Customer keeps unassigned
        notes outside the account count.</li>
        <li>Account names open focused account previews. Note dates open note-detail
        previews. Close a preview to keep your place, or choose Full Page for the complete
        record and editing. Modified clicks retain normal link navigation.</li>
        <li>Note preview shows up to 250 characters across opening paragraphs, so short
        greetings do not hide the substance. Topic matches are highlighted in both
        excerpts and in the note modal. Topic context shows the first
        paragraph containing the topic phrase, with matches highlighted. A message explains
        when the topic is already in the opening preview or is tagged without matching text.</li>
        <li>Choose All notes for the flat list. Your view choice is remembered across topics
        and app restarts. Back restores your view, expanded accounts and reading position.</li>
        <li>Newest first is the default within each account and the flat list. Sort Name, Note date, Seller, Territory,
        Topics or Partners in either direction. Back restores sorting and reading position.</li>
        <li>In All notes, View note opens the original note reader. Seller, Territory,
        Topics and Partners links open their original details; editing workflows are unchanged.</li>
        <li>Edit Topic opens the existing manual form. Delete asks for confirmation
        and removes the topic from associated notes without deleting the notes.
        Cancel or Escape leaves everything unchanged; failed deletion retains associations.</li>
      </ul>
    `},

    // ── Partners ──
    { pattern: /^\/partners\/?$/, title: 'Partners', content: `
      <p>Browse all partner organizations with their original ratings, specialties,
      contact counts and linked-note counts.</p>
      <ul>
        <li>Sort Name, Rating, Specialties, Contacts or Notes in either direction.
        Missing ratings stay last; zero is a real rating. Back restores sorting
        and reading position. The initial order is still alphabetical.</li>
        <li>Open a partner from its name or row, or follow a specialty directly.
        New Partner and Manage Specialties keep their existing workflows.</li>
        <li>Share Directory finds currently online teammates. Searching is local;
        data is sent only after you choose a recipient and they accept.
        Cancel or Escape clears the waiting share.</li>
        <li>Incoming partner data opens a native review with New, Updated and
        No Changes groups. Select All, Deselect All and Select New Only remain available.
        Import Selected is the explicit save; Cancel discards the review.</li>
        <li>Failed imports keep your selection for retry. Pending imports cannot
        be dismissed or submitted twice. Sharing exports original partner fields,
        not linked customer notes or contact photos.</li>
      </ul>
    `},
    { pattern: /^\/partners\/new\/?$/, title: 'New Partner', content: `
      <p>Name is required. Website, specialties, rating and notes are optional.
      Create Partner manually saves this organization and opens its details.</p>
      <ul>
        <li>Saving normalizes the website to its domain and fetches its favicon.</li>
        <li>Search specialties; arrow keys or Tab cycle suggestions and Enter selects.
        Create saves a specialty immediately, even if you cancel this partner.</li>
        <li>Rating is optional. Clear removes the rating; Notes is plain text.</li>
        <li>Cancel returns to Partners. Unsaved changes are guarded; Back restores
        this entry's fields, specialty selection and reading position without autosave.</li>
        <li>Failed saves retain entries for retry. Contacts are managed on Partner Details.</li>
      </ul>
    `},
    { pattern: /^\/partners\/\d+\/edit\/?$/, title: 'Edit Partner', content: `
      <p>Edit the original name, website, specialties, optional rating and plain-text notes.
      Save Changes manually saves the partner. Contacts and linked notes are preserved.</p>
      <ul>
        <li>Select or remove specialties locally. Create saves a new specialty immediately,
        independently of this partner's eventual Save or Cancel.</li>
        <li>Changing the website refreshes its favicon on Save. Clear removes a rating,
        not the partner's other information. Zero remains a valid existing rating.</li>
        <li>Cancel returns to Partner Details. Unsaved changes are guarded; Back restores
        fields, selections and reading position without autosave.</li>
        <li>Pending writes block duplicate submission and leaving. Failed writes keep
        entries for correction and retry. Contacts have separate controls on Partner Details.</li>
      </ul>
    `},
    { pattern: /^\/partners\/\d+/, title: 'Partner Details', content: `
      <p>Read partner notes beside contacts and associated notes. Specialties open their existing partner lists.</p>
      <ul>
        <li>Sort contact Name, Title, Email or Primary and linked-note Name or Note date
        in either direction. Back restores independent sorting and reading positions.</li>
        <li><strong>Edit</strong> opens the partner form. <strong>Add Contact</strong> and
        contact Edit use manual saves; unsaved entries are guarded and failures retain input.
        Set primary saves immediately. Delete commands require confirmation.</li>
        <li>Contact photos support clipboard screenshots and immediate removal. Pending
        writes block closing or switching contacts; failed requests can be retried.</li>
        <li><strong>Auto-fill from Meetings</strong> searches WorkIQ. If a domain is needed,
        Search Meetings saves the domain immediately, then searches. Review and edit notes,
        select new specialties or new/updated contacts, then Import Selected. Existing notes
        stay above the separator. Cancel does not import results; Try Again repeats the search.</li>
        <li><strong>Share</strong> uses the existing native sharing review with explicit send,
        recipient selection, cancellation and failure recovery.</li>
      </ul>
    `},

    // ── Specialties ──
    { pattern: /^\/specialties\/\d+\/?$/, title: 'Specialty Details', content: `
      <p>Read the full specialty description and all associated partners, with original
      contact and note counts. Partner names and rows open Partner Details.</p>
      <ul>
        <li>Sort Name, Contacts or Notes in either direction. Back restores sorting
        and both reading axes; the initial partner order remains alphabetical.</li>
        <li>Edit opens the existing manual form. Delete opens a native confirmation;
        Cancel keeps the specialty and every relationship unchanged.</li>
        <li>Deletion removes only the specialty and its associations, not partners,
        contacts or notes. Failed deletion rolls back and returns here with an error.</li>
      </ul>
    `},
    { pattern: /^\/specialties\/(?:new|\d+\/edit)$/, title: 'Specialty Form', content: `
      <p>Name is required; description is optional. Create Specialty and Save Changes
      save manually, then open the specialty. Cancel returns to Specialties.</p>
      <ul>
        <li>Names and descriptions are trimmed on save. Names must be unique,
        ignoring case; editing does not change the specialty's partner associations.</li>
        <li>Save and Cancel stay visible. Leaving with changes opens a discard guard;
        Back restores this entry's raw fields and reading position without autosave.</li>
        <li>Validation or failed saves retain your entries for correction and retry.</li>
        <li>Partner forms and the note's partner flyout create specialties immediately.
        Those separate writes are not undone by cancelling a partner.</li>
      </ul>
    `},
    { pattern: /^\/specialties\/?$/, title: 'Specialties', content: `
      <p>Browse all specialties with their descriptions and complete associated partner counts.</p>
      <ul>
        <li>Open a specialty from its name or row, or use Edit to change it. New Specialty creates a specialty through the existing manual form.</li>
        <li>Sort Name, Description or Partners in either direction. Description is shown when at least one specialty has one; full descriptions are available on hover and in detail.</li>
        <li>Back restores sorting and reading position. Header sorts are temporary, not saved preferences; the initial order remains the original database name order.</li>
      </ul>
    `},

    // ── Pods & Solution Engineers ──
    { pattern: /^\/pods\/?$/, title: 'PODs', content: `
      <p>Browse all PODs with their territory and SE counts, unique sellers and assigned solution engineers.</p>
      <ul>
        <li>Open a POD from its name or row. Seller and SE names retain their original roster scope.</li>
        <li>Sort Name, Territories, SEs, Sellers or Solution Engineers in either direction. Back restores sorting and reading position.</li>
        <li>The initial order remains alphabetical by POD name. POD membership is managed by the existing MSX sync, not a New POD command.</li>
      </ul>
    `},
    { pattern: /^\/pod\/\d+\/edit$/, title: 'Edit POD', content: `
      <p>POD name, territories and solution engineers remain managed by MSX sync.</p>
      <ul>
        <li>Specialist choices remain grouped by territory and specialty.
        A single specialist without an explicit preference is shown automatically.</li>
        <li>Changing a choice saves that assignment immediately. None clears the preference.
        There is no whole-POD submit or New POD workflow.</li>
        <li>Failed saves retain the selection and offer Retry. Pending saves block
        navigation; leaving failed drafts requires confirmation and never undoes saved assignments.</li>
        <li>Back restores this entry's reading position and unsaved choices without
        saving them. Back to POD opens the original POD details.</li>
      </ul>
    `},
    { pattern: /^\/pod\/\d+$/, title: 'POD Details', content: `
      <p>Read territory and seller coverage beside POD engineers and territory specialists.</p>
      <ul>
        <li>Territory customer counts use direct territory assignments. Seller counts
        include each seller's full coverage; sellers appear once across this POD's territories.</li>
        <li>Sort each record data heading in either direction. Back restores independent
        reading positions, sorting and the specialist disclosure.</li>
        <li>Only unselected multi-specialist assignments offer a choice here.
        Selecting saves immediately and becomes a static name; Edit POD changes existing
        preferences. Failed saves retain input with Retry and guarded navigation.</li>
        <li>Territory, seller and engineer links open their original details.
        POD identity and memberships remain managed by MSX sync.</li>
      </ul>
    `},
    { pattern: /^\/internal-contacts\/(?:new|\d+\/edit)\/?$/, title: 'Internal Contact Form', content: `
      <p>Name is required. Alias is optional and accepts the Microsoft alias or full
      Microsoft email. Role is free text; the original suggestions are optional.</p>
      <p>Create Contact or Save Changes saves explicitly and returns to Internal Contacts.
      Cancel returns to the directory. Nothing autosaves; leaving unsaved changes asks
      before discarding, and Back restores this entry's fields.</p>
      <p>Required-name and Edit duplicate-alias errors retain your entries for correction.
      Failed saves retain raw input for retry. Creating a duplicate alias opens the existing
      contact with its saved identity, without replacing it with the attempted new entries.</p>
      <p>On Edit, Delete opens a separate confirmation with Cancel selected.
      Confirming deletes the saved contact without saving the draft; existing note
      attendees retain its saved name and email.</p>
    `},
    { pattern: /^\/internal-contacts\/?$/, title: 'Internal Contacts', content: `
      <p>Microsoft employees not tracked as sellers or solution engineers.</p>
      <ul>
        <li>Open a name or row to edit the contact. New Contact opens the existing creation form.</li>
        <li>Sort Name, Alias or Role in either direction. Back restores sorting and reading position.</li>
        <li>All contacts remain visible regardless of your role or seller mode. There are no directory filters.</li>
        <li>Delete asks for confirmation and converts note attendee references to external attendees.
        Cancel or Escape leaves the contact unchanged. A failed deletion keeps the original records.</li>
      </ul>
    `},
    { pattern: /^\/solution-engineers\/?$/, title: 'Solution Engineers', content: `
      <p>All solution engineers, with their email, specialty and assigned PODs.</p>
      <ul>
        <li>Open an engineer's name or row to inspect their profile. Edit opens the existing editor.</li>
        <li>Sort Name, Email, Specialty or PODs in either direction. Back restores sorting and reading position.</li>
        <li>Unassigned engineers remain visible. This directory is not filtered by your role or seller mode.</li>
      </ul>
    `},
    { pattern: /^\/solution-engineer\/\d+\/edit\/?$/, title: 'Edit Solution Engineer', content: `
      <p>Edit the required name, optional Microsoft email alias, specialty and POD assignments.</p>
      <p><strong>Save Solution Engineer</strong> saves explicitly and opens the existing detail page.
      Cancel returns to the same engineer. Nothing autosaves; leaving unsubmitted changes asks
      before discarding, and Back restores this form's fields and POD selections.</p>
      <p>Validation and save failures keep your entries for correction or retry.
      This form does not edit direct territory assignments or run MSX sync.</p>
    `},
    { pattern: /^\/solution-engineer\/\d+/, title: 'Solution Engineer Details', content: `
      <p>Identity, assigned PODs and the unique territories across those PODs.
      Customer counts describe each territory's full coverage, not the engineer's direct
      territory assignments. Opening this page does not sync or change assignments.</p>
      <p>POD Name and territory Name/Customers headings sort both directions.
      Each coverage cell scrolls independently; Back restores its sorting and reading
      position. Email, POD and territory links keep their original destinations.</p>
      <p>Edit opens the existing manual-save form. Delete asks for confirmation,
      starts on Cancel and removes the engineer and POD associations only after submission.
      A failed deletion preserves the record and reports the failure for retry.</p>
    `},

    // ── Milestones ──
    { pattern: /^\/milestone\/new\/?$/, title: 'New Milestone', content: `
      <p>Add an MSX URL and an optional friendly title to your local milestones.</p>
      <p><strong>Create Milestone</strong> saves explicitly and opens the existing detail page.
      Cancel returns to Milestones. Leaving unsubmitted changes asks before discarding;
      Back preserves the form in its own history entry. Nothing autosaves to MSX.</p>
      <p>The MSX URL is required. Duplicate URLs and failed saves show an error and keep
      your fields available to correct or retry.</p>
    `},
    { pattern: /^\/milestone\/\d+\/edit\/?$/, title: 'Edit Milestone', content: `
      <p>Update the existing MSX URL or optional friendly title. Other milestone fields
      and relationships are unchanged.</p>
      <p><strong>Update Milestone</strong> saves explicitly. Cancel returns to this
      milestone's details. Unsubmitted changes are protected when leaving, and Back
      preserves the form in its own history entry. Nothing autosaves to MSX.</p>
      <p>The MSX URL is required. Duplicate URLs and failed saves show an error and keep
      your fields available to correct or retry.</p>
    `},
    { pattern: /^\/milestones\/?$/, title: 'Milestones', content: `
      <p>Browse customer-linked milestones from the local cache and open their linked calls and tasks.</p>
      <h6>Tips</h6>
      <ul>
        <li>The directory starts with the newest added records. Click Milestone, Source URL or Calls to sort either way; Back restores sorting and reading position.</li>
        <li>Open a record through its title, URL or call count. Source URLs remain visible in full on hover.</li>
        <li>Use <strong>Milestone Tracker</strong> for filters, calendar, exports and MSX sync, or <strong>New Milestone</strong> to create one manually.</li>
      </ul>
    `},
    { pattern: /^\/reports\/u2c/, title: 'U2C Attainment', content: `
      <p>Track uncommitted-to-committed milestones against an official MSXi quarterly snapshot.</p>
      <h6>Reading the measures</h6>
      <ul>
        <li><strong>Total Pipeline ACR</strong> is the snapshot ACR for all in-scope milestones.</li>
        <li><strong>Committed ACR</strong> uses snapshot ACR for milestones now committed.</li>
        <li><strong>U2C %</strong> divides committed snapshot ACR by total pipeline ACR, with a 40% goal.</li>
        <li><strong>Attainment %</strong> uses live ACR at commitment instead of snapshot ACR. It can differ from U2C %.</li>
        <li><strong>Remaining ACR</strong> is the snapshot ACR still uncommitted.</li>
      </ul>
      <h6>Scope and trend</h6>
      <p>Quarter selects a saved snapshot. <strong>Workloads</strong> lets you select multiple prefixes, such as Apps, Data, Infra and Sec. Each includes all its detailed workloads. No selection means all workloads. U2C remembers its own selection on this device without changing filters on other reports.</p>
      <p>Workloads filter the summary, queues and chart using snapshot classifications. Multiple workloads combine their dollar bases, not their percentages. Seller and customer filters affect summary totals and queues only; the chart remains workload-wide. Clear seller and customer does not clear workloads; use Show all workloads inside the workload picker.</p>
      <p>The chart keeps captured publications unchanged and shows only dates within the selected quarter. If the current payload has no matching publication date, an <strong>observed</strong> point saves that day's latest refresh without guessing a publish date. Earlier days remain saved. For a current non-final quarter, a separate <strong>Today (live)</strong> diamond uses current synced commitment status and milestone dollars from the latest imported snapshot, not current milestone ACR. A saved point on today's date is not replaced by Today. Values can fall when MSXi restates conversions. The dashed goal line follows the snapshot pipeline, not live ACR.</p>
      <p>Quarter rollover preserves the previous quarter and retries unfinished close-out. If MSXi no longer serves that quarter, its last saved data is retained, which can be older if the app was offline. Until new-quarter data is available, the previous quarter remains visible.</p>
      <h6>Milestone queues</h6>
      <p>Sort the six data columns with a click, Enter or Space. Engagement and 1:1 columns are actions, not sorts. Customer and milestone links open existing records; Back restores reading position and an unsubmitted 1:1 draft.</p>
      <p><strong>Add</strong> opens a discussion-note editor. Only <strong>Add to 1:1</strong> saves it to the seller workspace. Existing items show <strong>On 1:1</strong>.</p>
      <h6>Refresh</h6>
      <p><strong>Refresh from MSXi</strong> refreshes the current quarter using your configured territories, even when viewing an older snapshot. It does not commit milestones. A failed refresh leaves the last saved data available.</p>
    `},
    { pattern: /^\/reports\/activity-coverage/, title: 'Activity Coverage', content: `
      <p>Review every stored fiscal-year meeting, find activities already logged in MSX, and prepare missing activities without leaving Sales Buddy.</p>

      <h6>Meetings and milestones</h6>
      <p><strong>Meetings</strong> covers calendar activity one meeting at a time. <strong>Milestones</strong> finds active milestones where you are on team but have no HVA task during the current fiscal year. A prepared meeting draft does not count until its MSX activity is created.</p>
      <ul>
        <li><strong>Show covered</strong> adds milestones with a current-FY HVA task.</li>
        <li><strong>Show inactive</strong> adds completed, cancelled, and other inactive milestones.</li>
        <li><strong>HVA in FYxx</strong> preserves prior-year evidence while leaving the milestone uncovered for current-year reporting.</li>
        <li><strong>Create HVA Task</strong> creates a standalone open MSX task and never links it to a meeting.</li>
      </ul>

      <h6>Recommended workflow</h6>
      <ol>
        <li><strong>Catch Up Calendar</strong> imports calendar days after the last successful import through today. It adds meetings; it does not create MSX activities.</li>
        <li><strong>Match Milestones</strong> first runs the improved account sync once when needed, refreshes milestones and existing activities, then uses WorkIQ meeting context to prepare a subject, description, activity type, and suggested milestone for each unlogged customer meeting.</li>
        <li>Use <strong>Prepared</strong> and the chevron above the Date column to audit suggestions. Correct the customer, milestone, activity type, duration, or draft text when needed.</li>
        <li><strong>Create Activity</strong> creates one open MSX task for that meeting. Nothing is created until you click it.</li>
      </ol>

      <h6>Weekly and Full FY views</h6>
      <p><strong>Weekly</strong> keeps review batches small and provides week-by-week navigation. <strong>Full FY</strong> shows every fiscal-year meeting in one continuous list. Matching and calendar import operate across the fiscal year in either view; the toggle only changes what is displayed.</p>
      <p>The audit list scrolls independently, keeping lens controls and the status footer visible. The footer counts displayed records, not the fiscal-year coverage denominator. Back restores your reading position and expanded audit rows.</p>
      <p><strong>CAIP Coverage</strong> uses its own denominator: every on-team POC/Pilot and Production milestone. Its HVA evidence requires completed, due-dated tasks completed in the current fiscal year. It is not interchangeable with FY HVA Coverage.</p>

      <h6>Toolbar actions</h6>
      <table class="sb-help-table">
        <tbody>
          <tr><td><strong>Match Milestones</strong></td><td>Prepares unlogged meetings with WorkIQ and suggests milestones. Does not create MSX tasks.</td></tr>
          <tr><td><strong>Re-run Matching</strong></td><td>Replaces prior AI summaries, drafts, activity types, and milestone suggestions for all unlogged meetings. Customer matches, dismissals, notes, and linked MSX activities stay unchanged.</td></tr>
          <tr><td><strong>Catch Up Calendar</strong></td><td>Imports calendar meetings since the last completed day. Use it after time away or when recent days are missing.</td></tr>
        </tbody>
      </table>

      <h6>Reviewing a meeting</h6>
      <ul>
        <li><strong>Customer</strong> searches name, nickname, and TPID.</li>
        <li><strong>Milestone</strong> searches milestone name/number, opportunity, and workload. <span class="sb-help-indicator" data-tone="info">On Team</span> identifies milestones where you are on the team.</li>
        <li>A <strong>★</strong> beside an activity type means it qualifies for HVA credit. Preparation prefers a matching HVA type, then falls back to a more accurate non-HVA type when needed.</li>
        <li>Meeting draft edits save automatically; the inline save status confirms persistence without creating anything in MSX. Standalone HVA and prepared-meeting forms retain their explicit <strong>Save Draft</strong> commands.</li>
        <li><strong>Dismiss</strong> removes a meeting from coverage. Recurring meetings can be dismissed once or as a series.</li>
      </ul>

      <h6>Status and filters</h6>
      <ul>
        <li><strong>Logged</strong> has an existing linked MSX activity.</li>
        <li><strong>Ready</strong> has a customer and selected milestone and can be created after review.</li>
        <li><strong>Customer</strong> or <strong>Milestone</strong> needs manual matching.</li>
        <li><strong>Prepared</strong> finished WorkIQ preparation; <strong>Failed</strong> needs a retry.</li>
      </ul>
    `},
    { pattern: /^\/reports\/milestone-tracker/, title: 'Milestone Tracker', content: `
      <p>Review the existing local milestone cache in a sortable list or due-date calendar. Commands and scope stay visible while the reading region scrolls.</p>
      <ul>
        <li>Data headings sort both directions using raw dates, amounts and membership. The initial order stays exactly as supplied by the cache. Favorite and MSX action headings do not sort.</li>
        <li>CSV, Clipboard and Email export the filtered list in its current order, including original MSX source links. Calendar days are not sortable data columns.</li>
        <li>Back restores the filter scope, list/calendar selection, calendar month, sorting and reading position. Milestone previews reuse the shared detail, comment and nested task editors.</li>
        <li>Customer selection supports searchable choices, Enter to select and Escape to close without changing scope. Reset clears the existing filters, not your cached data.</li>
      </ul>
      <h6>Manual Sync</h6>
      <ul>
        <li>Click <strong>Sync from MSX</strong> to pull the latest data on demand</li>
        <li>Every sync refreshes <em>all</em> milestones and opportunities for every customer with an MSX account link</li>
        <li>Sync typically takes 6-10 minutes depending on how many customers you have</li>
      </ul>
      <h6>Scheduled Sync</h6>
      <ul>
        <li>A Windows scheduled task (<code>SalesBuddy-MilestoneSync</code>) runs automatically on Mon/Wed/Fri</li>
        <li>The sync time is randomly assigned between 9:30 AM and 4:30 PM when first registered, spreading load across users</li>
        <li>It only runs if Sales Buddy is already running - it won't start the server on its own</li>
        <li>The task is registered the first time you run <code>start.bat</code></li>
        <li>Enable or disable it from <strong>Admin Panel > Milestone Sync</strong> toggle</li>
      </ul>
      <h6>Tips</h6>
      <ul>
        <li>Use the filters to narrow by seller, status, quarter, or workload area</li>
        <li>Click any row to view milestone details</li>
        <li>The MSX icon opens the milestone directly in MSX</li>
      </ul>
    `},
    { pattern: /^\/opportunity\/\d+\/?$/, title: 'Opportunity Details', content: `
      <p>Read complete opportunity details beside linked milestones and forecast comments. Back restores both reading regions and milestone sorting; customer and MSX links keep their original destinations.</p>
      <p>Opening this page or its MSX Workspace preview reads fresh MSX details and comments and updates the local cache. Source and freshness stay visible; read failures retain cached information. No team, task or comment is saved merely by opening a detail.</p>
      <p>Sort milestone Name, Workload, Status, ACR / mo or Due date both ways using raw values. Authored forecast tables are content, not sortable work lists.</p>
      <p>Forecast Post, Edit and Delete, favorites and Join/Leave remain explicit writes. Leave defaults to Cancel. Unsaved comments offer Keep editing or Discard when leaving; pending writes must finish before closing or replacing a preview. Linked milestones retain their full notes, engagement and task workflows.</p>
    `},
    { pattern: /^\/milestone\/\d+/, title: 'Milestone Details', content: `
      <p>Review details, linked engagements, notes and tasks beside the forecast comments. Both reading regions scroll independently; Back restores each position while Open in MSX, Edit and Delete stay available.</p>
      <p>Opening an MSX-linked milestone reads fresh details and comments into the local cache. The status rail distinguishes loading, cached read failures, unsaved drafts and pending saves. Refresh details and comments remains a separate, explicit action.</p>
      <p>Comments and tasks save only when submitted. Leaving with a draft offers Keep editing or Discard; pending saves must finish first. Linked notes, team actions and task/engagement editors retain their original workflows. Deleting a milestone requires confirmation and defaults to Cancel.</p>
    `},

    { pattern: /^\/one-on-one\/?$/, title: '1:1 Workspaces', content: `
      <p>Your existing standing notes, next-conversation queues and discussion history, grouped by person.</p>
      <p>The default order is most recently updated first. Sort any of the six directory columns with a click, Enter or Space. Updated sorts by the displayed calendar date. Queue and discussion counts keep their existing definitions, including zero counts.</p>
      <p>Click any cell to open the same workspace. The person link is the keyboard entry point for each row. Standing context previews remain plain text, limited to 180 characters; open the workspace for the complete notes.</p>
      <p>Filter sellers in <strong>Start another workspace</strong>. This only narrows available seller quick starts, not the existing workspace directory. Opening a seller quick start creates or reuses that seller's scoped workspace. The Manager/Other form creates a workspace only when you click <strong>Create</strong>.</p>
      <p>Back restores seller filtering, table order, reading positions and an unsubmitted name/type draft. Embedded hubs keep their state local without changing the parent history.</p>
    `},
    { pattern: /^\/one-on-one\/\d+\/?$/, title: '1:1 Notes', content: `
      <p>Keep recurring context in <strong>Standing notes</strong> while the <strong>Next conversation</strong> agenda scrolls separately. The existing rich-text toolbar supports headings, lists, links, images and editable tables. The notes editor fills the available page height, with long notes scrolling beneath the fixed toolbar. Back restores both reading regions and the discussion-history disclosure.</p>
      <p>Standing notes and talking points save automatically. <strong>Not saved</strong> keeps the draft in place and offers <strong>Retry save</strong>. Navigation, agenda actions and closing an inline workspace wait for pending autosaves; unsent discussion notes and engagement edits remain protected.</p>
      <p><strong>Add to Agenda</strong> searches engagements or milestones. Seller workspaces stay scoped to that seller's customers; Manager/Other workspaces use the full book. Existing active items are excluded. Choosing a result adds it to this workspace.</p>
      <p>Use an item's actions menu to mark it discussed or remove it. <strong>Discussion history</strong> retains earlier talking points and dates, with Restore and Delete controls. Milestones can become linked engagements without losing their talking points. Engagements retain their inline details editor and discussion-note history; <strong>Save to engagement</strong> creates the linked note.</p>
    `},
    { pattern: /^\/reports\/(?:initiative-tracker|manager-one-on-one)\/?$/, title: 'Initiative Tracker', content: `
      <p>Curate named initiatives with context and selected engagements, milestones or internal projects. The initiative index jumps within the reading region; commands and save status stay visible.</p>
      <p>Work keeps its current seller grouping, status, commitment, due date and estimated monthly ACR. Sort Work, Customer, Status, Due or ACR in either direction without moving work between sellers. Points and removal are actions, not sorting columns.</p>
      <p><strong>Add work</strong> searches existing records and excludes work already added. Milestones also support <strong>From U2C</strong>, using this quarter's snapshot independently of filters on other reports. Already-added and not-yet-synced records cannot be selected. Add up to 75 results at a time.</p>
      <p>Open a work row for inline details or use <strong>Full Page</strong> for its source. Seller <strong>1:1 notes</strong> opens the existing workspace inline. Pending workspace edits must save before closing, replacing or leaving the detail.</p>
      <p><strong>Points</strong> saves current text automatically. <strong>Mark discussed</strong> retains a dated history with created and discussed timestamps. Failed saves keep drafts and expose Retry saving. Initiative forms stay in place on failure; leaving with unsaved forms warns you. Removing report links does not delete source work.</p>
      <p>Back restores the reading position, open editors, picker filters/selections and work-table sorts. Closing an initiative form keeps its unsaved fields; save the form before changing other report work. No export is offered on this curated tracker.</p>
    `},
    { pattern: /^\/reports\/one-on-one\/?$/, title: '1:1 Preparation', content: `
      <p>Prepare for a seller or manager conversation using recent activity and all open engagements.</p>
      <p>Choose <strong>Last 2 weeks</strong> or <strong>Last 30 days</strong> for monthly reviews. Recent customer activity, note and engagement counts, commitments, topic counts and reviewed revenue alerts use that rolling window. <strong>All Open Engagements</strong> shows Active engagements regardless of note dates. Switching activity tabs does not change highlight scopes.</p>
      <p><strong>Sort customer activity</strong> orders both customer lists by most recent call, estimated engagement ACR (highest first), seller or territory. ACR sums the engagements shown under each customer and also orders their engagements. Notes-only customers remain visible and unassigned sellers/territories sort last.</p>
      <p><strong>Commitments</strong> lists on-team milestones committed in the selected period. <strong>Due This Quarter</strong> also includes older overdue active milestones. Topic sparklines remain fiscal-year monthly counts.</p>
      <p>Sort any highlight column with a click, Enter or Space. Trend sorts by the fiscal-year note total. Long tables scroll independently. Expand customer groups or Notes to read the first five notes and follow existing records. Back restores the chosen view, disclosures, reading positions and table sorts.</p>
      <p><strong>Open 1:1 Notes</strong> opens existing workspaces, seller notes, or the explicit Manager/Other creation form. Opening this report never creates a workspace. <strong>Initiative Tracker</strong> opens your separate curated priorities.</p>
      <p>CSV, Copy to Clipboard and Email export every highlight table, including its offscreen rows and original milestone URLs. Customer activity lists are not included in these table exports.</p>
    `},
    { pattern: /^\/reports\/?$/, title: 'Reports', content: `
      <p>Open reports from the same groups and order used in the Reports menu.</p>
      <p>FY27 Data Motions brings together P to F, Fabric Consumption, Fabric Capacity Pressure, Fabric Modernization and Real-Time Intelligence.</p>
      <p>Use Find a report to match names, purposes or groups. Clear restores the full directory. The status bar shows how many reports match; filtering does not change report data.</p>
      <p>Each row opens its named report. Back restores your filter and reading position. Embedded directories keep their filter local and do not change the parent page's history.</p>
    `},
    { pattern: /^\/reports\/revenue\/?$/, title: 'Revenue Analyzer', content: `
      <p>Review saved revenue signals, ordered by existing priority, with product drill-downs, recommended actions and review history. Use Re-analyze to refresh the analysis after imports or configuration changes.</p>
      <p><strong>Database Expansion</strong> widget links open this analyzer's focused database view: Databases bucket, priority 20+, New or To be reviewed status, and Expansion Outreach or Check-in recommendations. Monitor-only, reviewed, actioned and dismissed alerts are excluded. Growth and consumption risks support the same motion but are not qualified opportunities.</p>
      <p>The focused view loads every matching alert before the normal top-50 cross-bucket limit. Dashboard customer/seller restrictions carry through, including an empty scope. It does not change your usual bucket selection. All revenue alerts returns to the normal analyzer and its saved bucket preferences.</p>
      <p>Open an alert for existing product details, then record review notes and set its status. Reviewed, actioned or dismissed signals leave the motion queue on its next load. CSV, Copy and Email export the current view. Risk and growth estimates are separate, not a net forecast.</p>
    `},
    // ── Revenue ──
    { pattern: /^\/revenue\/import/, title: 'Revenue Import', content: `
      <p>Pull ACR (Azure Consumed Revenue) straight from MSXI using your Azure sign-in.</p>
      <h6>Tips</h6>
      <ul>
        <li>Requires the corporate VPN - no export or file needed</li>
        <li>Each sync replaces the stored revenue with a fresh two-year pull</li>
        <li>The status band updates automatically and shows active progress, completed syncs and failures. Returning to this page reconnects to an active sync.</li>
        <li>After a completed sync, use Refresh stored-data tables to update history and month coverage. Revenue also appears on customer and seller pages.</li>
        <li>Every history and month column sorts in both directions; each pane scrolls independently.</li>
        <li>Select Your Buckets filters revenue views across the app and saves a server fallback. No selection shows every bucket. Failed reads or saves offer Retry.</li>
        <li>Bucket-change notices remain until the server confirms dismissal.</li>
      </ul>
    `},
    { pattern: /^\/revenue\/config/, title: 'Revenue Configuration', content: `
      <p>Tune the nine thresholds used by Revenue Analyzer. Revenue gates reduce noise, dollar overrides force material alerts, tiers boost account priority, and category thresholds adjust sensitivity.</p>
      <p>Dollar fields use whole dollar amounts. Drop and growth fields use decimal ratios, not whole percentages: -0.15 means a 15% drop; 0.08 means 8% growth.</p>
      <p>Save Configuration stores the settings and returns to Revenue Analyzer. Saving does not re-run analysis. Use Re-analyze there to apply changes immediately, or wait for the next analysis run.</p>
      <p>Cancel returns to Revenue Analyzer without submitting the form. The command band remains available while the editor scrolls.</p>
    `},
    { pattern: /^\/reports\/msx-workspace/, title: 'MSX Workspace', content: `
      <p>Browse the existing local MSX cache in Opportunities, Milestones and Tasks.
      Milestone Sync opens the existing sync workspace; opening this browser does not run a sync.</p>
      <ul>
        <li>Choose seller or customer and enter a keyword, then Apply. Clear restores Open opportunities and the three active milestone statuses.</li>
        <li>Opportunity search matches name, number and owner. Milestone search matches title, number, workload and owner. Task search matches subject and category.</li>
        <li>Each tab retains its team/status controls. Selecting no milestone statuses keeps the original unrestricted status scope.</li>
        <li>Milestone <strong>Workloads</strong> supports multiple prefixes such as Apps, Data, Infra and Sec, including all their detailed workloads. MSX Workspace remembers its own selection on this device, independent of other reports and Back history. No selection means all workloads. A selected prefix absent from the current scope stays restrictive and is labeled not in this view.</li>
        <li>Click a data heading to sort in either direction. Dates and ACR use full raw values; team headings sort membership, not button labels. The untouched default order is preserved.</li>
        <li>Expand an opportunity for cached description, customer need and compete threat. View Details retains the existing fresh MSX details and forecast-comment editor.</li>
        <li>A milestone title opens the shared details/HoK workflow. Its task icon narrows Tasks to that milestone and enables Create Task. Back to Milestones keeps your reading position.</li>
        <li>Unsubmitted task drafts stay in their dialog, including after a failed creation. Finish or clear a draft before changing milestone scope. Pending writes block dismissal.</li>
      </ul>
      <p>Back restores filters, unsubmitted search text, the selected tab, sorting and independent reading positions. The workspace has no table export command.</p>
    `},
    { pattern: /^\/reports\/workload/, title: 'Workload Report', content: `
      <p>Customers grouped by workload topic. Use this to find accounts working on a specific technology for workshop targeting or account team questions.</p>
      <h6>How it works</h6>
      <ul>
        <li>Topics from your notes are used as workload tags</li>
        <li>Select a workload on the left to see its customers on the right. Embedded reports use expandable workload groups.</li>
        <li>Click any column heading to sort either direction. Selection does not change date scope or totals.</li>
        <li><strong>Committed</strong> means at least one active milestone linked to a note with this topic is committed</li>
        <li><strong>Last Activity</strong> shows the most recent note date for that topic + customer</li>
      </ul>
      <h6>Column definitions</h6>
      <ul>
        <li><strong>Engagements</strong> - active engagements for this customer (all, not topic-scoped)</li>
        <li><strong>Milestones</strong> - active milestones linked to notes that are tagged with this topic. A milestone appears here because you wrote a note about it and tagged it with this workload.</li>
        <li><strong>Est. ACR</strong> - sum of monthly usage from those topic-linked milestones only (not the customer's full ACR)</li>
      </ul>
      <h6>Date filter</h6>
      <p>From/To filters each customer's <strong>latest note for that topic</strong>, inclusively. Earlier notes in the range do not qualify an entry whose latest note is outside it. Counts and ACR still describe the linked active engagements and milestones, not activity recomputed within the range.</p>
      <p>Dates default to the current fiscal quarter. Presets, including All, are saved between visits. Editing dates clears the active preset. Invalid ranges leave the previous valid scope visible. Storage failures have Retry; choosing a preset replaces a malformed saved filter.</p>
      <p>Unique Customers counts distinct customer names. Customer/workload entries counts appearances across topics. Total ACR deduplicates milestone IDs across visible workloads; individual workload sums can overlap.</p>
      <p>CSV, Clipboard and Email include all workloads matching the date scope, not just the selected workload. Back restores date scope, selected workload, sorting and reading positions. Embedded reports do not change their parent's view history.</p>
    `},
    { pattern: /^\/reports\/hygiene/, title: 'Engagement / Milestone Hygiene', content: `
      <p>Two repair queues show active engagements with no linked milestones and active customer milestones with no linked engagements. Any existing milestone link, including a completed milestone, removes an engagement from its gap queue.</p>
      <ul>
        <li>Switch between Engagement gaps and Milestone gaps. Data columns sort in either direction; Fix is a plain action heading.</li>
        <li>On Team only and Group by Seller affect the milestone queue. The top milestone total includes all team statuses.</li>
        <li>Reason notes save on blur, Enter or after 2 seconds. Only confirmed notes update the annotated count and exports. Not saved offers Retry note, preserving your latest text.</li>
        <li>Add Milestones links an engagement. Add to Engagement retains its existing links. Create Engagement starts with the source milestone selected; you can change that selection.</li>
        <li>Repairs refresh both queues only after confirmation. A failed refresh offers a read-only Retry. If creation cannot be confirmed, review the customer engagements before attempting it again.</li>
        <li>Unsaved reason notes must be saved before repairing. Closing an edited repair asks before discarding your fields or selection.</li>
      </ul>
      <p>CSV, Clipboard and Email include both queues, not just the selected tab, respecting On Team only. Grouped exports identify each seller in the milestone heading. Exports contain confirmed reasons and story percentages, without save-status or action-button text.</p>
      <p>Back restores the queue, scope, grouping, sorting and reading positions. Embedded reports show both queues stacked and do not change their parent's view history.</p>
    `},
    { pattern: /^\/reports\/whats-new/, title: "What's New", content: `
      <p>Created and Updated are independent activity lists. Lookback is a rolling window of 1 to 90 days; Last 24 hours is not calendar midnight. Explicit days in the URL override the saved lookback.</p>
      <p>MSX creation and modification dates take precedence over local dates. Newly created milestones normally appear under Created; records with incomplete MSX date metadata can appear in both lists. Counts describe list entries, not unique milestones across both panes.</p>
      <ul>
        <li>Seller, Area and Team filter both lists. Group by seller / customer and each group's expansion are saved between visits. Counts include collapsed groups.</li>
        <li>Change types filter Updated only. Select all removes that restriction; Clear all shows no Updated matches. Matching labels come from all cached audits, not just audits inside the lookback.</li>
        <li>When only some change types are selected, Updated shows the latest matching audit date, which can be older than the lookback. With all types selected, it returns to the milestone modification date. Sorting follows the displayed calendar date.</li>
        <li>Click any data heading to sort ascending or descending within its group. ACR sorts by full precision, not rounded display amounts. On Team sorts membership, not the action label.</li>
        <li>Join and leave require confirmation. Only confirmed changes update every occurrence, counts, filters and exports. An uncertain response is not automatically repeated; review the milestone in MSX first.</li>
      </ul>
      <p>CSV, Clipboard and Email include both lists' expanded rows in their current order, retaining section, seller and customer context. Collapsed rows are excluded. Back restores scope, grouping, sorts and each pane's reading position. Embedded reports stack the lists without changing their parent's view history.</p>
    `},
    { pattern: /^\/reports\/whitespace/, title: 'Whitespace Analysis', content: `
      <p>Analysis views answer different adoption questions. They intentionally use different time scopes.</p>
      <ul>
        <li>Whitespace uses the source's SuperStrategicPillars: AI, Analytics, Apps, Databases, Developer, Infra and Security. Pillar/product data refreshes with the regular ACR import, independently of Revenue Analyzer service buckets.</li>
        <li>Whitespace Grid uses the last three complete calendar months, dividing totals by 3. Missing months count as $0 and the current partial month is excluded, matching acquisition, modernization and RTI monthly ACR. Changing the spend-column checkboxes automatically updates the grid.</li>
        <li>Focus whitespace selects a pillar to target and excludes customers with coverage in that pillar. Customers need positive coverage in at least one other pillar. The focused pillar is hidden from the grid; clearing or changing focus restores it if selected. Spend columns only changes the surrounding context while focused, so hiding a column does not change the candidate list. Group by seller organizes those candidates using the existing checkbox. Include fully covered customers is disabled while a focus is active.</li>
        <li>With Any selected pillar, multiple spend columns show customers with some, but not all, selected pillars. Include fully covered customers adds the remaining spenders. A single pillar shows its spenders. Customers with no spend in any selected pillar are not part of this view.</li>
        <li>Initial selection includes all seven pillars. This report does not change the saved Revenue filter.</li>
        <li>The Database whitespace dashboard widgets require positive AI, Analytics or Apps coverage and no Databases coverage, using this grid's recent window with no additional spend cutoff. Developer, Infra and Security alone do not qualify a customer. Summary counts the same candidates as Details, which ranks monthly AI, Analytics (including Fabric) and Apps spend and totals only those three pillars. Both support customer/seller scope and seller grouping. The widget link opens that same candidate list with AI, Analytics and Apps columns, hiding the zero-only Databases column. Changing the spend columns does not change this linked Database cohort. Normal report visits remember your focus, spend columns and sorts. This linked visit does not overwrite those preferences; Back retains changes within the visit.</li>
        <li>Reverse Lookup searches name, nickname or TPID after two characters. Arrow keys and Enter select a match. Missing pillars and products use positive spend at any time in imported history, not the grid's recent window.</li>
        <li>Pillar Penetration counts historical positive spend. Its denominator includes every customer with imported pillar revenue, including zero-only customers. Click a pillar for the with-spend and without-spend lists.</li>
        <li>Fabric Acquisition is available for Data SEs and sellers. It shows positive AI, Databases or Other Analytics spend, with no positive monthly net Fabric ACR observed in the last three complete calendar months. Other Analytics excludes Fabric Capacity from the source Analytics pillar, but includes ADF where the source assigns it to Analytics. All amounts are three-month totals divided by 3. Missing revenue observations count as $0. All customers with no Fabric spend includes all current customers regardless of other spend, even without any imported revenue records. Accounts flagged stale after dropping out of account sync are excluded from the report and widgets, without deleting their saved data. No observed revenue does not establish licensing or non-adoption. Dashboard Summary and Details widgets include only customers with at least $2,000/month combined AI, Databases and Other Analytics ACR, using unrounded amounts. The report has no $2,000 minimum and the widget links directly to this broader tab.</li>
        <li>Data headings sort both directions using raw values. Hover or keyboard-focus a spend amount for its product-level average monthly ACR, including credits, over the same period. Click a grid amount for existing product details and full history without losing this report.</li>
      </ul>
      <p>Whitespace Grid and Fabric Acquisition each have a Group by seller toggle. Groups use current local ownership, keep unassigned customers together, and sort customers within each seller. CSV, Clipboard and Email export only the loaded grid, in its current order and coverage scope, for the original analysis views. On Fabric Acquisition, they export that queue instead, with monthly ACR and period labels. Group headings are included when grouping is enabled. Pillar selections and column sorting are saved on this device and restored on future visits. Sorts are kept separately for each set of columns. Back also restores the analysis view, each view's seller grouping and reading positions. Failed reads or preference saves offer Retry.</p>
    `},
    { pattern: /^\/reports\/connect-impact/, title: 'Connect Impact', content: `
      <p>Customers rank by total estimated monthly ACR from committed milestones you are on the team for. Committed since uses the commitment date, inclusively, not the due date. All execution statuses qualify, including Completed and Cancelled.</p>
      <ul>
        <li>Choose a customer to inspect its milestones; its heading opens the customer page. Click a milestone title for the shared milestone preview.</li>
        <li>Every data heading sorts both directions using raw amounts and calendar dates. Rank retains the original ACR ranking when you sort another column. Milestone amounts initially sort highest first; unavailable amounts contribute zero.</li>
        <li>The start date is remembered between visits. An explicit URL date overrides that preference; the initial default is five months ago, with the day capped at 28.</li>
        <li>Quick dates fills the start date from the current Microsoft fiscal year: Q1 / fiscal-year start is July 1, Q2 October 1, Q3 January 1 and Q4 April 1. These are committed-since shortcuts, not whole-quarter filters. Custom date focuses the date picker.</li>
        <li>Back retains the original date scope, selected customer, table sorts and both reading positions. Embedded reports do not change the saved date or their parent's history.</li>
      </ul>
    `},
    { pattern: /^\/reports\/marketing-insights/, title: 'Marketing Insights', content: `
      <p>Choose a customer in the left list to inspect its account summary, sales-play breakdown and engaged contacts. The report uses cached MSX marketing data; sync and re-sync remain in Admin.</p>
      <ul>
        <li>Filter matches the customer's display name, case-insensitively. Report totals always cover all synced customers, not just filtered matches.</li>
        <li>Every data heading sorts both directions. Counts use raw numbers; Last Date uses its UTC timestamp rather than a formatted local label. Unavailable values stay last.</li>
        <li>Top Engaged Contacts retains the existing ten contacts with the highest combined email and meeting interactions. Account engaged-contact totals can therefore exceed the displayed list.</li>
        <li>CSV, Clipboard and Email include the filtered customer summary in its current order, not the selected customer's detail tables. Customer names link to their local records.</li>
        <li>Click the selected customer's heading to open its record. Back restores the filter, selection, per-table sorting and independent reading positions.</li>
      </ul>
      <p>Summary-only accounts remain selectable. Empty results and incomplete or failed syncs are explicit. Embedded reports stack the chooser and details without owning the page frame.</p>
    `},
    { pattern: /^\/reports\/revenue-reports/, title: 'Revenue Reports', content: `
      <p>Open Fabric Modernization to compare recent source-product and Fabric spend grouped by seller. Revenue Analyzer returns to the broader revenue review.</p>
    `},
    { pattern: /^\/reports\/rti(?:\/|$)/, title: 'Real-Time Intelligence', content: `
      <p>Use Group by seller to switch between seller sections and one sortable customer list. The page URL retains your selection on reload and Back. CSV, Clipboard and Email export the currently displayed tables.</p>
      <p>Current accounts qualify with zero RTI usage and positive net Event Hubs, Stream Analytics or IoT revenue. Missing RTI measurements count as 0, so accounts without Fabric capacity are included. This can be an entry point to Fabric or a new workload for an existing Fabric customer. Customers already using RTI are excluded. No minimum spend cutoff applies.</p>
      <p>RTI CU-hours and RTI workload share come from Fabric 360's native rolling 28-day measurements. They are customer-wide, not per capacity.</p>
      <p>Dollar amounts are average monthly ACR across the last three complete calendar months: three-month totals divided by 3. The current month is excluded. Missing revenue observations count as $0. Credits are included. IoT includes IoT-prefixed products and Device Update for IoT Hub. Event Grid and unrelated products are excluded.</p>
      <p>Accounts that dropped out of account sync are excluded without deleting their saved history. Customer links open Fabric details; Total / mo links open customer Revenue. Sortable headings and exports retain source precision and customer links.</p>
      <p>Summary and Details dashboard widgets use the same queue, with customer/seller scope, optional seller grouping and automatic sync of both sources. Signals support discovery, not architecture assessments, external Kafka inventory or migration recommendations.</p>
      <p>Tables show customer, streaming ACR and the combined monthly total. RTI usage is an eligibility filter, not a redundant zero-only column. Create an engagement for a surfaced insight and track trial-to-F-SKU conversion manually; this queue does not track conversion outcomes automatically.</p>
    `},
    { pattern: /^\/reports\/fabric-consumption(?:\/|$)/, title: 'Fabric Consumption', content: `
      <p>Capacity overview offers customers with capacities, Trial audit, Trial to Paid audit and F-SKU audit. Search, One row per customer and Group by seller work with each audit. Use the separate P to F report for P-SKU renewal audits.</p>
      <p>Trial to Paid audit includes customers with FTL4 or FTL64 and no paid F-number SKU. P-SKU customers remain eligible. The Trial to Paid dashboard widget uses the same eligibility and links here. The broader Trial audit also includes customers already running paid F SKUs.</p>
      <p>Both trial audits retain other capacity context and average trial utilization in customer mode. The widget's Getting started and Building momentum queues use its existing saved 15-day history and 0.1% cutoff.</p>
      <p>A trial in the latest synced snapshot is active for this workflow. Create an engagement from a surfaced insight and track conversion to an F SKU manually; neither the widget nor audit detects conversion outcomes automatically.</p>
    `},
    { pattern: /^\/reports\/fabric-modernization/, title: 'Fabric Modernization', content: `
      <p>One row per customer with positive net revenue in Synapse, Azure Analysis Services (AAS), Azure Data Explorer (ADX), Azure Databricks or Azure Data Factory (ADF) over the last three complete calendar months.</p>
      <p>Other Analytics combines Synapse, AAS, ADX and Azure Databricks revenue, excluding ADF and Fabric. ADF is a separate column, covering Azure Data Factory v2, Data Flow and Managed Airflow. Synapse-branded pipelines stay under Synapse, without double-counting as ADF. Eligibility requires combined Other Analytics plus ADF three-month totals of at least $50, or at least $100 when Fabric revenue is positive. Cutoffs use unrounded net amounts, including credits.</p>
      <p>All displayed dollar amounts are average monthly ACR: each customer's net total divided by 3, with missing months contributing $0. The current partial month, future months and older history are excluded. Hover or keyboard-focus an Other Analytics amount for its exact product breakdown, including credits, using that same period and divisor.</p>
      <p>Fabric revenue indicates an observed paid footprint, not proof of migration or readiness. No observed revenue is not proof of no adoption. Azure Databricks does not establish non-Azure Databricks inventory.</p>
      <p>Partial coverage remains recorded; it does not change the fixed three-month divisor. Eligibility cutoffs continue to use the saved three-month totals.</p>
      <p>The Fabric modernization dashboard widget includes qualifying customers whose three-month Other Analytics plus ADF spend is strictly greater than Fabric spend, including ADF-only prospects. Summary shows their count and separate average monthly Other Analytics and ADF ACR, summing the customer-level averages even when month counts differ. Details shows Customer, Other Analytics, ADF and Fabric. A dotted underline on an Other Analytics amount identifies positive net Synapse spend; hover to see "Includes Synapse". These amounts describe existing consumption, not projected incremental Fabric benefit. Both presentations follow dashboard customer and seller scope. Use the dashboard's Group by seller viewer filter to show seller names.</p>
      <p>Click any table heading to sort ascending or descending within its seller group. Revenue sorts at source precision, not rounded display values.</p>
      <p>Customer links open matched Revenue pages. CSV, Clipboard and Email export all seller tables in their visible order; formatted exports retain account links.</p>
    `},
    { pattern: /^\/reports\/revenue\/?$/, title: 'Revenue Dashboard', content: `
      <p>Overview of Azure Consumed Revenue across your customers — flagging who needs attention and why.</p>
      <h6>Categories</h6>
      <table class="sb-help-table">
        <tbody>
          <tr>
            <td><span class="sb-help-indicator" data-tone="danger">Declining</span></td>
            <td>Revenue is declining month-over-month <em>and</em> dropped sharply recently. These customers may be migrating away or shutting down workloads.</td>
          </tr>
          <tr>
            <td><span class="sb-help-indicator">Recent Dip</span></td>
            <td>Overall trend was stable or positive, but there was a sudden drop in the last 1-2 months. Could be a billing anomaly, a paused project, or the start of something worse.</td>
          </tr>
          <tr>
            <td><span class="sb-help-indicator" data-tone="success">EXPANSION</span></td>
            <td>Revenue is growing with positive momentum — customer is near or at their historical max. Good time to engage and help them scale further.</td>
          </tr>
          <tr>
            <td><span class="sb-help-indicator" data-tone="neutral">Volatile</span></td>
            <td>Revenue swings significantly month to month (high coefficient of variation). Hard to predict — worth a conversation to understand usage patterns.</td>
          </tr>
          <tr>
            <td><span class="sb-help-indicator" data-tone="info">Stagnant</span></td>
            <td>Revenue is flat with low variance. Customer is using Azure but not growing. May be an opportunity to introduce new services.</td>
          </tr>
          <tr>
            <td><span class="sb-help-indicator" data-tone="neutral">Healthy</span></td>
            <td>No concerning signals — revenue is stable with normal patterns. No action needed.</td>
          </tr>
          <tr>
            <td><span class="sb-help-indicator" data-tone="info">New Customer</span></td>
            <td>Recently started generating revenue (first few months of usage). Worth an early check-in to ensure onboarding is going well.</td>
          </tr>
          <tr>
            <td><span class="sb-help-indicator" data-tone="neutral">Inactive</span></td>
            <td>Revenue dropped to $0 in this bucket after previously generating usage. The customer stopped using this specific product/service.</td>
          </tr>
        </tbody>
      </table>
      <h6>Tips</h6>
      <ul>
        <li>The <strong>Priority</strong> score (0-100) ranks how urgently a customer needs attention</li>
        <li>The <strong>$ Impact</strong> column shows estimated monthly revenue at risk or opportunity</li>
        <li>Every alert column sorts ascending or descending through its heading. Alert previews use the same sortable product grids, with summary totals pinned.</li>
        <li>Click a customer to see their full revenue breakdown with monthly charts</li>
        <li>Use <strong>Re-analyze</strong> after a fresh import to update all categories</li>
      </ul>
    `},
    { pattern: /^\/revenue\/seller\/[^/]+\/products\/?$/, title: 'Seller Products', content: `
      <p>Browse this seller's products beside their customer consumption. Choose a product on the left, then a customer entry to open the seller's recent-month comparison.</p>
      <p>Search product names, usage counts or totals. Every column sorts in both directions. Usage counts retain customer/bucket pairs and consolidated member counts.</p>
      <p>Open product reads the full monthly grid. Back restores product selection, search, sorting and pane positions. Embedded views remain compact lists.</p>
    `},
    { pattern: /^\/revenue\/seller\/[^/]+\/product\//, title: 'Seller Product Comparison', content: `
      <p>Compare this seller's customer and bucket entries across the seven most recent imported months. Total revenue includes all imported months, not only the displayed months.</p>
      <p>Search customer, bucket or amount; Clear restores every entry. Every column sorts ascending or descending without changing monthly comparisons or totals.</p>
      <p>Customer and bucket links open their Revenue pages when matched. View All Customers opens the global product workbench; Back to Products returns to the seller's browser. Consolidated products disclose every member.</p>
    `},
    { pattern: /^\/revenue\/seller\//, title: 'Seller Revenue Alerts', content: `
      <p>Revenue alerts for this seller's customers, initially sorted by priority. Review each alert and mark it as actioned, reviewed, or dismissed.</p>
      <p>Use Table view to sort any column in either direction; the alert queue follows that order. Product columns in the detail pane also sort both ways, with totals pinned.</p>
      <h6>Alert Categories</h6>
      <table class="sb-help-table">
        <tbody>
          <tr><td><span class="sb-help-indicator" data-tone="danger">Declining</span></td><td>Revenue is trending down with a recent sharp drop. Customer may be migrating away or reducing workloads.</td></tr>
          <tr><td><span class="sb-help-indicator">Recent Dip</span></td><td>Overall trend was stable but there was a sudden recent drop. Could be temporary or the start of something bigger.</td></tr>
          <tr><td><span class="sb-help-indicator" data-tone="success">Expansion</span></td><td>Growing usage with positive momentum. Good time to engage about scaling.</td></tr>
          <tr><td><span class="sb-help-indicator" data-tone="neutral">Volatile</span></td><td>Large month-to-month swings. Worth understanding what drives the variation.</td></tr>
        </tbody>
      </table>
    `},

    // ── Search ──
    { pattern: /^\/search/, title: 'Search', content: `
      <p>Search note content and combine the existing customer, seller, territory and topic filters. Search submits explicitly; changing a filter does not run a search.</p>
      <h6>Scope and results</h6>
      <ul>
        <li>Search Text matches a case-insensitive substring of stored note content, not customer names or engagement stories.</li>
        <li>Customer, Seller and Territory use customer ownership. Topics match any selected topic; hold Ctrl/Cmd to select several.</li>
        <li>Results keep Seller / Customer grouping, with newest notes within each customer. Open a note to read its full body.</li>
        <li>Seller mode keeps its seller scope, including after Clear Filters. General notes can match text or topics outside seller/customer/territory scope.</li>
        <li>Clear Filters returns to Search. With no active criteria, Recently Viewed and Recently Updated retain their existing links and timestamps.</li>
        <li>Back restores unsubmitted controls and reading position. There are no date filters, relevance ranking, pagination or exports on this page.</li>
      </ul>
    `},

    // ── Analytics ──
    { pattern: /^\/analytics/, title: 'Analytics', content: `
      <p>Charts and metrics about your note-taking activity, customer coverage, and team engagement.</p>
    `},

    // ── Settings ──
    { pattern: /^\/preferences/, title: 'Settings', content: `
      <p>Your personal Sales Buddy preferences — display options, defaults, and account settings.</p>
    `},

    // ── Fill My Day ──
    { pattern: /^\/fill-my-day/, title: 'Fill My Day', content: `
      <p>Import external meetings for a selected date into customer notes.</p>
      <h6>Tips</h6>
      <ul>
        <li>Fetch Meetings requests WorkIQ meetings, matches customer names and nicknames, then runs summaries and AI enrichment.</li>
        <li>Review note HTML, toggle suggested topics, and inspect the matched milestone. Unmatched meetings remain in a sortable disclosure.</li>
        <li>Create Task in MSX is an immediate remote write. Save Note creates a local note and may join the milestone team. Neither happens automatically.</li>
        <li>Done Editing applies HTML to this review, not to a saved note. Full Editor opens the original note form with customer and date.</li>
        <li>Back restores the review without repeating requests. Fetching again replaces unsaved review edits after confirmation.</li>
      </ul>
    `},

    // ── Connect Export ──
    { pattern: /^\/connect-export/, title: 'Connect Export', content: `
      <p>Export your notes in Connect-compatible format for uploading to the Connect system.</p>
    `},

    // ── Admin ──
    { pattern: /^\/admin\/ai-logs/, title: 'AI Logs', content: `
      <p>View recent AI gateway calls — prompts, responses, tokens used, and latency.</p>
    `},
    { pattern: /^\/admin\/favicons/, title: 'Favicon Management', content: `
      <p>View and refresh favicons for customers with websites. Useful if icons are missing or outdated.</p>
    `},
    { pattern: /^\/admin/, title: 'Admin Panel', content: `
      <p>System administration — AI settings, data management, and application configuration.</p>
    `},
  ];

  // ── Fallback help ───────────────────────────────────────────────────

  const fallbackHelp = {
    title: 'Sales Buddy Help',
    content: `
      <p>Sales Buddy helps Azure technical sellers capture and retrieve customer call notes.</p>
      <h6>Keyboard Shortcuts</h6>
      <table class="sb-help-table">
        <tbody>
          <tr><td><span class="kbd">F1</span></td><td>Page help (you're here!)</td></tr>
          <tr><td><span class="kbd">/</span></td><td>Quick customer lookup</td></tr>
          <tr><td><span class="kbd">Ctrl</span>+<span class="kbd">K</span></td><td>New note</td></tr>
          <tr><td><span class="kbd">Ctrl</span>+<span class="kbd">Enter</span></td><td>Submit form</td></tr>
          <tr><td><span class="kbd">/</span> or <span class="kbd">?</span></td><td>Customer search / Keyboard shortcuts</td></tr>
        </tbody>
      </table>
      <p class="sb-help-muted">Press <span class="kbd">F1</span> on any page for help specific to that page.</p>
    `,
  };

  // ── Public API ──────────────────────────────────────────────────────

  function show() {
    const path = window.location.pathname;

    // Find matching help content
    let help = null;
    for (const entry of helpMap) {
      if (entry.pattern.test(path)) {
        help = entry;
        break;
      }
    }

    const title = help ? help.title : fallbackHelp.title;
    const content = help ? help.content : fallbackHelp.content;

    // Populate and toggle the shared native help dialog.
    const titleEl = document.getElementById('pageHelpModalLabel');
    const bodyEl = document.getElementById('pageHelpBody');
    if (!titleEl || !bodyEl) throw new Error('Page help dialog content is missing');

    titleEl.textContent = title;
    bodyEl.innerHTML = content;
    bodyEl.scrollTop = 0;

    SBDialog.toggle('pageHelpModal');
  }

  return { show };
})();
