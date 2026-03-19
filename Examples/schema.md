## A. Compression Decisions

**Things found → what happens to each:**

- **People** → Own node type: **Person**. Every named individual gets a node. Title and email are additional properties, not separate types, since they're attributes of the person, not independent entities.
- **Companies, universities, advocacy groups, government bodies** → Merged into one node type: **Organization**, with an `organization_type` property to discriminate. They all share the same connectivity pattern (people work for them, they partner with each other, they operate in locations). For investigation purposes, treating a university and a company as the same node class lets you trace influence across sectors.
- **Roles (CEO, CFO, Board Member, etc.)** → Folded into a property (`role_title`) on the EMPLOYED_BY edge. Roles don't have independent graph connectivity — they only matter in the context of who holds them at which organization. Creating a "CEO" node that multiple people link to would obscure rather than reveal investigative patterns.
- **Meetings** → Own node type: **Meeting**. Critical for investigation — they are the venues where decisions are made, conflicts are disclosed, and people co-occur. Date, time, and venue are properties.
- **Documents (press releases, memos, emails, minutes, news articles)** → Own node type: **Document**, with a `document_type` property. All share the same connectivity (authored by someone, reference people and organizations). The whistleblower complaint is a Document with `document_type = "whistleblower_complaint"`.
- **Products/technologies (ZenSense-X, Health Insights, analytics platform, bikes)** → Own node type: **Product**, with `product_type` to distinguish sensors from services from vehicles. They share connectivity (developed by an org, supplied to another org).
- **Locations/cities** → Own node type: **Location**. Essential for cross-border investigation. Country is an additional property. Specific addresses are folded into meeting venue properties rather than getting their own Location nodes (an address is not a reusable entity in this corpus).
- **Personal/familial relationships** → Captured via **PERSONAL_RELATIONSHIP** edge between Person nodes, with `relationship_type` property (married, friend, family, roommate, in-law, nephew). This is the backbone of the conflict-of-interest analysis.
- **Financial deals (equity stakes, service fee schedules, revenue-sharing proposals)** → Own node type: **FinancialArrangement**. These are central to investigation — they represent the monetary flows that personal relationships might improperly influence. Type discriminated by `arrangement_type` property.
- **Action items/decisions** → Own node type: **ActionItem**. Investigation needs to trace who was assigned what task and by which meeting. Due date is a property.
- **Regulations (GDPR, APPI)** → Own node type: **Regulation**. Lightweight but important for tracking compliance obligations across jurisdictions — directly relevant to cross-border investigation.
- **Metrics/performance data (failure rates, revenue figures, margins)** → Folded into properties on relevant nodes and edges. A sensor failure rate is a property on a Document or FinancialArrangement discussion, not an independent entity. These figures lack stable identity for deduplication.
- **Fee schedule line items (individual service pricing rows)** → Folded into FinancialArrangement properties. The arrangement itself is the entity; individual line items are details within it.
- **Shared hobbies, personal anecdotes (marathon running, café visits)** → Captured as context in PERSONAL_RELATIONSHIP edge properties. Not worth their own nodes, but the relationship itself is preserved.

---

## B. Domain Description

This corpus consists of press releases, corporate meeting minutes, internal memos, business emails, and news reports produced by and about a cluster of three companies — a data analytics firm, an electric micro-mobility operator, and a biotechnology sensor manufacturer — along with their academic partners, regulators, and potential vendors. The documents span approximately two years and track an evolving cross-border business partnership involving equity investment, technology supply, joint product development, and service commercialisation across the Netherlands, the United States, and Japan.

The most distinctive feature of this corpus is the unusually dense web of personal and familial relationships among the principals: spousal ties between co-executives, in-law connections to partner CEOs, university friendships predating business dealings, and sibling relationships bridging partner companies. These interpersonal ties are explicitly discussed in meeting minutes, disclosed in conflict-of-interest statements, and challenged in a whistleblower complaint. The knowledge graph must foreground these person-to-person relationships alongside the formal business structure.

Financial arrangements — equity stakes, analytics service charges, revenue-sharing proposals, and vendor pricing — appear in structured tables and negotiation memos. Meetings are the primary decision-making venues, producing action items assigned to named individuals with due dates. Documents themselves carry investigative value: who authored them, who received them, and which people and organisations they reference. Cross-border dimensions (locations of meetings, headquarters, and operations across multiple countries) and regulatory compliance obligations (European and Japanese data protection law) are explicitly tracked throughout.

---

## C. Recommended Schema

```json
{
  "nodes": [
    {
      "node_class": "Entity",
      "type": "Person",
      "primary_property": "full_name",
      "qualifier": null,
      "additional_properties": ["title", "email"]
    },
    {
      "node_class": "Entity",
      "type": "Organization",
      "primary_property": "organization_name",
      "qualifier": null,
      "additional_properties": ["organization_type", "founding_year"]
    },
    {
      "node_class": "Event",
      "type": "Meeting",
      "primary_property": "meeting_title",
      "qualifier": null,
      "additional_properties": ["date", "time", "venue"]
    },
    {
      "node_class": "Document",
      "type": "Document",
      "primary_property": "document_title",
      "qualifier": null,
      "additional_properties": ["date", "document_type"]
    },
    {
      "node_class": "Asset",
      "type": "Product",
      "primary_property": "product_name",
      "qualifier": null,
      "additional_properties": ["product_type", "description", "unit_price"]
    },
    {
      "node_class": "Location",
      "type": "Location",
      "primary_property": "location_name",
      "qualifier": null,
      "additional_properties": ["country"]
    },
    {
      "node_class": "Event",
      "type": "ActionItem",
      "primary_property": "action_description",
      "qualifier": null,
      "additional_properties": ["due_date"]
    },
    {
      "node_class": "Asset",
      "type": "FinancialArrangement",
      "primary_property": "arrangement_name",
      "qualifier": null,
      "additional_properties": ["amount", "currency", "arrangement_type", "effective_date"]
    },
    {
      "node_class": "Concept",
      "type": "Regulation",
      "primary_property": "regulation_name",
      "qualifier": null,
      "additional_properties": ["jurisdiction"]
    }
  ],
  "edges": [
    {
      "start_node_type": "Person",
      "end_node_type": "Organization",
      "type": "EMPLOYED_BY",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["role_title", "start_date"]
    },
    {
      "start_node_type": "Person",
      "end_node_type": "Organization",
      "type": "FOUNDED",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["founding_year"]
    },
    {
      "start_node_type": "Person",
      "end_node_type": "Person",
      "type": "PERSONAL_RELATIONSHIP",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["relationship_type", "context"]
    },
    {
      "start_node_type": "Person",
      "end_node_type": "Meeting",
      "type": "ATTENDED",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["role_in_meeting", "disclosed_conflict"]
    },
    {
      "start_node_type": "Person",
      "end_node_type": "ActionItem",
      "type": "RESPONSIBLE_FOR",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": []
    },
    {
      "start_node_type": "Person",
      "end_node_type": "Document",
      "type": "AUTHORED",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": []
    },
    {
      "start_node_type": "Person",
      "end_node_type": "Document",
      "type": "RECEIVED",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["receipt_type"]
    },
    {
      "start_node_type": "Document",
      "end_node_type": "Meeting",
      "type": "RECORDS_MEETING",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": []
    },
    {
      "start_node_type": "Document",
      "end_node_type": "Person",
      "type": "REFERENCES_PERSON",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["context"]
    },
    {
      "start_node_type": "Document",
      "end_node_type": "Organization",
      "type": "REFERENCES_ORGANIZATION",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["context"]
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Organization",
      "type": "INVESTED_IN",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["stake_percentage", "amount", "currency", "date"]
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Organization",
      "type": "SUPPLIES_TO",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["product_supplied", "quantity", "contract_value"]
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Organization",
      "type": "PARTNERS_WITH",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["partnership_type", "start_date"]
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Organization",
      "type": "EVALUATED_AS_VENDOR",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["evaluation_outcome", "evaluation_date"]
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Location",
      "type": "HEADQUARTERED_IN",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": []
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Location",
      "type": "OPERATES_IN",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["operation_type"]
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Product",
      "type": "DEVELOPS",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": []
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "Regulation",
      "type": "SUBJECT_TO",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["compliance_status"]
    },
    {
      "start_node_type": "Organization",
      "end_node_type": "FinancialArrangement",
      "type": "PARTY_TO",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["role_in_arrangement"]
    },
    {
      "start_node_type": "Meeting",
      "end_node_type": "Location",
      "type": "HELD_AT",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": []
    },
    {
      "start_node_type": "Meeting",
      "end_node_type": "ActionItem",
      "type": "PRODUCED_ACTION",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": []
    },
    {
      "start_node_type": "Meeting",
      "end_node_type": "FinancialArrangement",
      "type": "APPROVED_ARRANGEMENT",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["vote_outcome"]
    }
  ]
}
```

**Notes on each type:**

**Nodes:**

| Type | What it represents / absorbs | Why this primary_property | Key additional_properties |
|---|---|---|---|
| **Person** | All named individuals (executives, employees, academics, analysts). Absorbs roles as a property on EMPLOYED_BY rather than separate nodes. | `full_name` — stable, canonical, how people are identified on first mention in every document | `title` captures their most recent known title; `email` for communication tracing |
| **Organization** | Companies, universities, NGOs, advocacy groups, government bodies. Discriminated by `organization_type`. | `organization_name` — the legal/common name, always stated explicitly | `organization_type` distinguishes companies from universities from advocacy groups; `founding_year` for timeline |
| **Meeting** | Board meetings, steering committees, strategy sessions. The key decision-making events. | `meeting_title` — constructed from org + date + purpose, always derivable from the minutes header | `date` and `venue` are essential for cross-border timeline analysis |
| **Document** | Press releases, memos, emails, minutes, news articles. The evidence trail itself. | `document_title` — the subject line or headline, always present | `document_type` discriminates press release from email from whistleblower complaint |
| **Product** | Sensors (ZenSense-X), services (Health Insights), platforms, vehicles. | `product_name` — the brand/product name, always stated | `product_type` separates hardware from software from services; `unit_price` for financial tracking |
| **Location** | Cities where operations, meetings, and headquarters exist. | `location_name` — city name, the natural identifier | `country` is essential for cross-border jurisdiction analysis |
| **ActionItem** | Tasks and decisions from meetings, with assigned owners and deadlines. | `action_description` — the task text from decision tables | `due_date` for tracking follow-through and accountability |
| **FinancialArrangement** | Equity investments, service agreements, revenue-sharing deals, fee schedules. Merged because they share the same connectivity (orgs as parties, meetings as approval venues). | `arrangement_name` — a descriptive label for the deal | `arrangement_type` discriminates investment from service contract from revenue share; `amount`/`currency` for financial tracking |
| **Regulation** | GDPR, APPI, and any other regulatory framework mentioned. | `regulation_name` — the standard abbreviation/name | `jurisdiction` maps to cross-border compliance obligations |

**Edges:**

| Edge | Purpose for investigation |
|---|---|
| EMPLOYED_BY | Maps who holds what role where — the org chart |
| FOUNDED | Traces founders, relevant to control and influence |
| PERSONAL_RELATIONSHIP | The core conflict-of-interest substrate. `relationship_type` distinguishes married/friend/family/roommate/in-law |
| ATTENDED | Who was in the room when decisions were made. `disclosed_conflict` flags whether they declared a conflict |
| RESPONSIBLE_FOR | Who was tasked with follow-up — accountability trace |
| AUTHORED | Who created each document — authorship chain |
| RECEIVED | Who received each document (to/cc/bcc via `receipt_type`) — information flow |
| RECORDS_MEETING | Links minutes to the meeting they document |
| REFERENCES_PERSON / REFERENCES_ORGANIZATION | What people and orgs a document discusses — the mention graph, critical for tracing who is discussed where |
| INVESTED_IN | Equity relationships between orgs, with financial details |
| SUPPLIES_TO | Supply chain relationships — who depends on whom |
| PARTNERS_WITH | Formal partnership declarations |
| EVALUATED_AS_VENDOR | Vendor evaluation process — traces whether alternatives to connected parties were genuinely considered |
| HEADQUARTERED_IN / OPERATES_IN | Jurisdiction and cross-border footprint |
| DEVELOPS | Which org makes which product |
| SUBJECT_TO | Regulatory obligations by jurisdiction |
| PARTY_TO | Which orgs are parties to which financial arrangement |
| HELD_AT | Where meetings took place — cross-border movement tracking |
| PRODUCED_ACTION | Which meeting generated which tasks |
| APPROVED_ARRANGEMENT | Which meeting approved which deal — the decision audit trail |

---

## Step 4 — Review

Review the schema above:

- Any categories I dropped or merged that you need kept separate? (For instance, I folded roles into edge properties rather than giving them their own node type, and I merged all financial deal types into one FinancialArrangement node.)
- Any relationships missing that matter for your investigation? (For example, I didn't create a dedicated edge for "recused from voting" — that's captured as a property on ATTENDED. Should it be its own edge?)
- Want me to shift the budget? (Currently at 9 node types and 22 edge types — there's room to add more of either.)

Once you're satisfied, you can:
1. Copy the JSON and paste it into the Upload Schema tab in Phase 1
2. Copy the domain description and use the AI Generate tab instead
