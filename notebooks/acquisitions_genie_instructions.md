# Acquisitions Genie space instructions

Space `Acquisitions`. These are the per-table `description` rules pushed to
`core_dev.techops.content_acquistions_priority_titles` in the space's `serialized_space` (per-table description is the
field Genie follows; see CLAUDE.md). Keep this file in sync when the live instructions change.

Note: "acquistions" is misspelled in the table name. Keep it exactly as is.

Source: Airtable export of content acquisition deals, entered by the Partnerships team (partly automated) and loaded by a weekly notebook run. Each refresh replaces the previous data, so row counts change week to week. There is no load-date column, so there is no week-over-week history.

## Rules

1. The table is refreshed weekly. Figures reflect the latest refresh only. Always calculate from the data; never reuse numbers from earlier answers. The table cannot answer week-over-week or trend questions.
2. One row is one title or episode, in one country, for one license window. The title column can hold episode names (e.g. "SHOW -- S01E03"). The same deal can appear on several identical rows.
3. To count titles, use COUNT(DISTINCT ltrim('0', program_id)). Some program_ids have a leading zero (0300021967 and 300021967 are the same title). Never count rows to answer title questions.
4. FEES MUST BE DEDUPLICATED. total_fee is repeated on duplicate rows, so a plain SUM(total_fee) overstates spend (about 13% today). For ANY question about spend, cost, fees or budget, sum from this deduplicated set and never from the raw table:

```sql
WITH fees AS (
  SELECT * FROM core_dev.techops.content_acquistions_priority_titles
  QUALIFY row_number() OVER (
    PARTITION BY ltrim('0', program_id), exploited_country, start_date, end_date, total_fee
    ORDER BY created, id) = 1
)
SELECT ... SUM(total_fee) ... FROM fees ...
```

   Apply every filter and GROUP BY in the outer query, after the dedup. Title counts can use the raw table or `fees`; both give the same distinct titles. Say in the answer that fees are deduplicated. The dedup counts a repeated fee once per title, country and window, so genuine same-fee splits are counted once and the total can slightly understate.
5. Do not use duplicate_entry to remove rows or to deduplicate. It is TRUE or NULL (never FALSE), is not consistent, and does not catch every duplicate. Use the dedup in rule 4.
6. total_fee is a row's own fee for its own term. monthly_fee = total_fee / term, where term is the license length in months. A $0 total_fee is not a free title: the fee often sits on one territory's row and other rows show 0.
7. fee_match has the values TERRITORY FEE, MATCH and DISCREPANCY. Do not interpret what they mean and do not filter on them unless the user asks. If asked about DISCREPANCY, say its meaning is not defined in the data and the Partnerships team owns it.
8. title_modified, start_date_modified, end_date_modified, last_modified, last_modified_2 and date_changed are timestamps of record edits, not alternate values. Do not use them in place of title, start_date or end_date.
9. start_date and end_date are text in yyyy-MM-dd format. Always convert them with to_date().
10. A window is active when to_date(start_date) <= current_date() AND to_date(end_date) >= current_date().
11. An end_date in 2099 or later means a perpetual license.
12. "Expiring" means to_date(end_date) falls between current_date() and the requested horizon.
13. "Added recently" uses created (when the record was created). "Recently updated" uses last_modified (when it was last edited).
14. deal_type, status, financial_category and exclusivity_dropdown are arrays that usually hold one value. Read them with [0], e.g. deal_type[0].
15. Deal types: Cash, Recoup_On_Pub, Recoup_On_Gross.
16. For exclusivity, use exclusivity_dropdown[0] (Non-Exclusive, Original - True Exclusive, Fully Exclusive, AVOD Exclusive, Co-Exclusive, FAST Exclusive). About 3% of rows have no value (or occasionally two): label NULL as 'Unspecified' with coalesce(exclusivity_dropdown[0], 'Unspecified'). Do not use the free-text exclusivity column.
17. territory_mapping holds regions and some individual countries as SEPARATE values: US, CA, UK, AU, MX, Central America, NZ, EC, SV, GT, CR, PA. NZ is not folded into AU, and EC, SV, GT, CR and PA are not folded into Central America. For "Australia/NZ" use territory_mapping IN ('AU', 'NZ'). For "Central America" including those countries use territory_mapping IN ('Central America', 'SV', 'GT', 'CR', 'PA', 'EC'). exploited_country holds the country code and uses GB rather than UK. Use territory_mapping for regions and exploited_country for individual countries.
18. tvt_tier sometimes has a trailing space; use trim(tvt_tier). Match language case-insensitively (both "English" and "ENGLISH" exist).
19. budget_category: Library, Shiny, Original, Elite, Niche. strategic_vertical: Audience Engagement, Creator Content, Younger Audiences, Sports Culture; about 20% of rows are NULL, so label them "Unassigned". A few duplicate rows disagree on these labels; the dedup keeps the earliest-created row.
20. cap_lead, genre, primary_genre, vertical and studio_indie are arrays. Use array_contains() to filter and explode() to group.
21. studio holds the partner/studio import ID (e.g. sony-pictures, warner-bros).
22. New values can appear after a refresh. When filtering on a category, check the values currently in the data rather than relying only on the lists above.
23. Filter categories and country codes with exact equality, not LIKE or ILIKE: exploited_country = 'US', budget_category = 'Original', territory_mapping = 'US'. Pattern matches such as '%US%' can match unrelated values. Only use ILIKE for language and free-text title searches.
