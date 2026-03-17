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
    // ── Home ──
    { pattern: /^\/$/, title: 'Dashboard', content: `
      <p>Your home base — a quick overview of recent activity across Sales Buddy.</p>
      <h6>Quick actions</h6>
      <ul>
        <li>Use <span class="kbd">Ctrl</span>+<span class="kbd">K</span> to jump to any customer</li>
        <li>Use <span class="kbd">Ctrl</span>+<span class="kbd">Shift</span>+<span class="kbd">N</span> to create a quick note</li>
        <li>Click any card to dive into that section</li>
      </ul>
    `},

    // ── Notes ──
    { pattern: /^\/notes\/?$/, title: 'Notes', content: `
      <p>All your call notes in one place. Each note captures what was discussed, which technologies came up, and who was involved.</p>
      <h6>Tips</h6>
      <ul>
        <li>Use the filters at the top to narrow by customer, seller, topic, or date range</li>
        <li>Click a note title to view the full note with all details</li>
        <li><strong>New Note</strong> — attach it to a customer and tag relevant topics</li>
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
    { pattern: /^\/customers\/?$/, title: 'Customers', content: `
      <p>Your customer directory. Each customer can have notes, engagements, opportunities, and revenue data linked to them.</p>
      <h6>Tips</h6>
      <ul>
        <li>Use the search bar to filter by name</li>
        <li>Click a customer to see all their linked notes, engagements, and revenue</li>
        <li><span class="kbd">Ctrl</span>+<span class="kbd">K</span> opens quick customer lookup from any page</li>
      </ul>
    `},
    { pattern: /^\/customer\/new/, title: 'New Customer', content: `
      <p>Add a new customer to track. You'll be able to attach notes, engagements, and revenue data to them.</p>
      <h6>Tips</h6>
      <ul>
        <li>The <strong>name</strong> is the only required field</li>
        <li>Adding a territory helps with filtering and reporting later</li>
      </ul>
    `},
    { pattern: /^\/customer\/\d+\/edit/, title: 'Edit Customer', content: `
      <p>Update this customer's information — name, territory, sellers, and other details.</p>
    `},
    { pattern: /^\/customer\/\d+/, title: 'Customer Details', content: `
      <p>Everything about this customer — notes, engagements, opportunities, and revenue all in one place.</p>
      <h6>Actions</h6>
      <ul>
        <li><strong>New Note</strong> — add a call note linked to this customer</li>
        <li><strong>New Engagement</strong> — create a tracked engagement</li>
        <li>Scroll down to see revenue data and engagement history</li>
      </ul>
    `},

    // ── Engagements ──
    { pattern: /^\/engagement\/\d+\/edit/, title: 'Edit Engagement', content: `
      <p>Update this engagement's details, status, and linked milestones.</p>
    `},
    { pattern: /^\/engagement\/\d+/, title: 'Engagement Details', content: `
      <p>Track a specific customer engagement — its status, linked notes, milestones, and AI-generated story.</p>
      <h6>Actions</h6>
      <ul>
        <li><strong>Generate Story</strong> — AI creates a summary from linked call notes</li>
        <li><strong>Link Notes</strong> — attach relevant call notes to this engagement</li>
        <li><strong>Milestones</strong> — track this engagement against MSX milestones</li>
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
    { pattern: /^\/seller\/\d+/, title: 'Seller Details', content: `
      <p>This seller's profile — linked notes, customers, territory, and activity.</p>
    `},

    // ── Territories ──
    { pattern: /^\/territories\/?$/, title: 'Territories', content: `
      <p>Organize your customers and sellers by territory (region, segment, etc.).</p>
      <h6>Tips</h6>
      <ul>
        <li>Territories help filter and group across the app</li>
        <li>Assign territories to customers and sellers for better organization</li>
      </ul>
    `},
    { pattern: /^\/territory\/\d+/, title: 'Territory Details', content: `
      <p>All customers and sellers in this territory, plus linked notes and activity.</p>
    `},

    // ── Topics ──
    { pattern: /^\/topics\/?$/, title: 'Topics', content: `
      <p>Topics are the technologies and themes discussed in your calls (e.g., "Fabric", "Copilot Studio", "Migration").</p>
      <h6>Tips</h6>
      <ul>
        <li>Tag notes with topics to make them searchable</li>
        <li>Click a topic to see all notes where it was discussed</li>
        <li>The AI can auto-suggest topics when you create a note</li>
      </ul>
    `},
    { pattern: /^\/topic\/\d+/, title: 'Topic Details', content: `
      <p>All notes tagged with this topic, plus associated customers and sellers.</p>
    `},

    // ── Partners ──
    { pattern: /^\/partners\/?$/, title: 'Partners', content: `
      <p>Your partner directory — ISVs, SIs, and other organizations you work with.</p>
      <h6>Tips</h6>
      <ul>
        <li>Rate partners (1-5 stars) based on your experience</li>
        <li>Add <strong>specialties</strong> to track what each partner is good at</li>
        <li>Add <strong>contacts</strong> with email and phone for quick reference</li>
        <li><strong>Share Directory</strong> — send your partner list and saved details to another Sales Buddy user who is currently online (both users must be running Sales Buddy at the same time)</li>
      </ul>
    `},
    { pattern: /^\/partners\/new/, title: 'New Partner', content: `
      <p>Add a new partner organization to your directory.</p>
      <h6>Tips</h6>
      <ul>
        <li>Add a <strong>website</strong> and we'll grab the favicon automatically</li>
        <li>Tag relevant <strong>specialties</strong> so you can find them later</li>
        <li>Add contacts — mark one as <strong>primary</strong> for quick access</li>
      </ul>
    `},
    { pattern: /^\/partners\/\d+\/edit/, title: 'Edit Partner', content: `
      <p>Update this partner's details, contacts, specialties, and rating.</p>
    `},
    { pattern: /^\/partners\/\d+/, title: 'Partner Details', content: `
      <p>Full profile for this partner — overview, contacts, specialties, and linked notes.</p>
      <h6>Actions</h6>
      <ul>
        <li><strong>Share</strong> — send this partner to a teammate via Socket.IO</li>
        <li><strong>Edit</strong> — update details, add contacts</li>
        <li>Click specialties to see other partners with the same skills</li>
      </ul>
    `},

    // ── Specialties ──
    { pattern: /^\/specialties\/?$/, title: 'Specialties', content: `
      <p>Manage the list of specialties that can be assigned to partners (e.g., "Migrations", "Purview", "Copilot Studio").</p>
    `},

    // ── Pods & Solution Engineers ──
    { pattern: /^\/pods\/?$/, title: 'Pods', content: `
      <p>Pods are groups of sellers that work together. Great for organizing team coverage.</p>
    `},
    { pattern: /^\/pod\/\d+/, title: 'Pod Details', content: `
      <p>Members of this pod and their collective activity.</p>
    `},
    { pattern: /^\/solution-engineers\/?$/, title: 'Solution Engineers', content: `
      <p>Your solution engineering team. SEs provide technical depth on customer calls.</p>
    `},
    { pattern: /^\/solution-engineer\/\d+/, title: 'Solution Engineer Details', content: `
      <p>This SE's profile — linked notes, customers, and specialties.</p>
    `},

    // ── Milestones ──
    { pattern: /^\/milestones\/?$/, title: 'Milestones', content: `
      <p>Track MSX milestones and their linked engagements.</p>
      <h6>Tips</h6>
      <ul>
        <li>Milestones sync with MSX via AI-generated engagement stories</li>
        <li>Use the <strong>Milestone Tracker</strong> for a board view</li>
      </ul>
    `},
    { pattern: /^\/milestone-tracker/, title: 'Milestone Tracker', content: `
      <p>Board view of all milestones across your customers, pulled from MSX.</p>
      <h6>Manual Sync</h6>
      <ul>
        <li>Click <strong>Sync from MSX</strong> to pull the latest data on demand</li>
        <li>Every sync refreshes <em>all</em> milestones and opportunities for every customer with an MSX account link</li>
        <li>Sync typically takes 6-10 minutes depending on how many customers you have</li>
      </ul>
      <h6>Scheduled Sync</h6>
      <ul>
        <li>A Windows scheduled task (<code>SalesBuddy-MilestoneSync</code>) runs automatically on Mon/Wed/Fri at 3:00 PM</li>
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
    { pattern: /^\/milestone\/\d+/, title: 'Milestone Details', content: `
      <p>This milestone's tasks, linked engagements, and MSX sync status.</p>
    `},

    // ── Revenue ──
    { pattern: /^\/revenue\/import/, title: 'Revenue Import', content: `
      <p>Import ACR (Azure Consumed Revenue) data from CSV exports.</p>
      <h6>Tips</h6>
      <ul>
        <li>Use the standard ACR export format from the revenue portal</li>
        <li>Import is additive — existing data won't be overwritten</li>
        <li>After import, revenue will appear on customer and seller pages</li>
      </ul>
    `},
    { pattern: /^\/revenue\/config/, title: 'Revenue Configuration', content: `
      <p>Configure revenue tracking settings — product groupings, alert thresholds, and seller mappings.</p>
    `},
    { pattern: /^\/revenue\/reports/, title: 'Revenue Reports', content: `
      <p>Pre-built reports for revenue analysis — new users, growth trends, and product adoption.</p>
    `},
    { pattern: /^\/revenue\/?$/, title: 'Revenue Dashboard', content: `
      <p>Overview of Azure Consumed Revenue across your customers — flagging who needs attention and why.</p>
      <h6>Categories</h6>
      <table class="table table-sm mb-3">
        <tbody>
          <tr>
            <td><span class="badge bg-danger">CHURN_RISK</span></td>
            <td>Revenue is declining month-over-month <em>and</em> dropped sharply recently. These customers may be migrating away or shutting down workloads.</td>
          </tr>
          <tr>
            <td><span class="badge bg-warning text-dark">RECENT_DIP</span></td>
            <td>Overall trend was stable or positive, but there was a sudden drop in the last 1-2 months. Could be a billing anomaly, a paused project, or the start of something worse.</td>
          </tr>
          <tr>
            <td><span class="badge bg-success">EXPANSION</span></td>
            <td>Revenue is growing with positive momentum — customer is near or at their historical max. Good time to engage and help them scale further.</td>
          </tr>
          <tr>
            <td><span class="badge bg-secondary">VOLATILE</span></td>
            <td>Revenue swings significantly month to month (high coefficient of variation). Hard to predict — worth a conversation to understand usage patterns.</td>
          </tr>
          <tr>
            <td><span class="badge bg-info text-dark">STAGNANT</span></td>
            <td>Revenue is flat with low variance. Customer is using Azure but not growing. May be an opportunity to introduce new services.</td>
          </tr>
          <tr>
            <td><span class="badge bg-light text-dark">HEALTHY</span></td>
            <td>No concerning signals — revenue is stable with normal patterns. No action needed.</td>
          </tr>
          <tr>
            <td><span class="badge bg-primary">NEW_CUSTOMER</span></td>
            <td>Recently started generating revenue (first few months of usage). Worth an early check-in to ensure onboarding is going well.</td>
          </tr>
          <tr>
            <td><span class="badge bg-dark">CHURNED</span></td>
            <td>Revenue dropped to $0 after previously generating usage. Customer has stopped using Azure entirely.</td>
          </tr>
        </tbody>
      </table>
      <h6>Tips</h6>
      <ul>
        <li>The <strong>Priority</strong> score (0-100) ranks how urgently a customer needs attention</li>
        <li>The <strong>$ Impact</strong> column shows estimated monthly revenue at risk or opportunity</li>
        <li>Click a customer to see their full revenue breakdown with monthly charts</li>
        <li>Use <strong>Re-analyze</strong> after a fresh import to update all categories</li>
      </ul>
    `},

    // ── Search ──
    { pattern: /^\/search/, title: 'Search', content: `
      <p>Full-text search across all your notes, customers, and engagements.</p>
      <h6>Tips</h6>
      <ul>
        <li>Search by keyword, customer name, topic, or seller</li>
        <li>Use filters to narrow results by type</li>
        <li>Results are ranked by relevance</li>
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
      <p>Meeting prep tool — quickly review customer context and past notes before your calls today.</p>
      <h6>Tips</h6>
      <ul>
        <li>Syncs with your calendar to show upcoming meetings</li>
        <li>Click a meeting to see relevant notes and customer history</li>
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
      <table class="table table-sm mb-3">
        <tbody>
          <tr><td><span class="kbd">F1</span></td><td>Page help (you're here!)</td></tr>
          <tr><td><span class="kbd">Ctrl</span>+<span class="kbd">K</span></td><td>Quick customer lookup</td></tr>
          <tr><td><span class="kbd">Ctrl</span>+<span class="kbd">Shift</span>+<span class="kbd">N</span></td><td>New note</td></tr>
          <tr><td><span class="kbd">Ctrl</span>+<span class="kbd">Enter</span></td><td>Submit form</td></tr>
          <tr><td><span class="kbd">/</span> or <span class="kbd">?</span></td><td>Keyboard shortcuts</td></tr>
        </tbody>
      </table>
      <p class="text-muted mb-0">Press <span class="kbd">F1</span> on any page for help specific to that page.</p>
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

    // Populate and show the modal
    const titleEl = document.getElementById('pageHelpModalLabel');
    const bodyEl = document.getElementById('pageHelpBody');
    if (!titleEl || !bodyEl) return;

    titleEl.innerHTML = `<i class="bi bi-question-circle"></i> ${title}`;
    bodyEl.innerHTML = content;

    const modal = bootstrap.Modal.getOrCreateInstance(
      document.getElementById('pageHelpModal')
    );
    modal.toggle();
  }

  return { show };
})();
