# Carelog: Product Context Document

Last updated: April 2026

---

## What Carelog is

Carelog is a compliance software product for Australian residential aged care providers. It automates the specific regulatory reporting tasks that the Aged Care Act 2024 requires, starting with care minutes tracking, SIRS (Serious Incident Response Scheme) incident reporting, and audit-ready document generation.

Carelog is not a clinical care platform. It is not an electronic health record. It is not a rostering tool. It sits on top of whatever systems a provider already uses (HumanForce, iCare, spreadsheets, whatever) and pulls the data those systems hold into the specific outputs the regulator wants. The value proposition is simple: the Aged Care Quality and Safety Commission asks providers for specific reports in specific formats on specific deadlines. Carelog generates those reports automatically instead of someone spending hours assembling them by hand.

The target customer is the small-to-mid residential aged care provider running 1 to 10 facilities. These organisations carry the same compliance obligations as Opal or Estia (the largest operators in the country) but do not have compliance teams, IT departments, or the budget for enterprise software. They are currently managing compliance with spreadsheets, disconnected tools, and senior staff time.

---

## The problem

### The regulatory change

On 1 November 2025, the Aged Care Act 2024 came into force, replacing the Aged Care Act 1997. This was not a routine update. It was the most significant structural reform to aged care regulation in Australian history, triggered by the Royal Commission into Aged Care Quality and Safety (2018-2021), which found the system was "not fit for purpose."

The new Act introduced:

- Personal criminal liability for governing body members (Directors of Nursing, board members)
- 7 new Strengthened Quality Standards replacing the old 8
- Expanded SIRS reporting to cover home care (previously residential only)
- 9 reportable incident categories with strict deadlines (Priority 1 within 24 hours)
- Civil penalties of up to $1.65 million for body corporates
- New powers for the Aged Care Quality and Safety Commission including unannounced visits and conditions on registration
- A data-driven risk-based supervision model where the Commission cross-references care minutes, SIRS reports, complaints, quality indicators, and staffing data to flag providers before visiting

### Care minutes: the specific pain

The most operationally painful requirement is care minutes. Every residential aged care facility must deliver a sector-wide average of 215 minutes of direct care per resident per day, including 44 minutes from a registered nurse. These are reported quarterly through the Quarterly Financial Report.

Key facts about care minutes:

- The target is adjusted per facility based on the AN-ACC (Australian National Aged Care Classification) case mix of residents
- Facilities that miss targets by more than 5% for two consecutive quarters trigger a regulatory response
- The Commission has already issued enforceable undertakings to 11 providers operating 27 homes
- As of 2024-25, only 45.9% of residential aged care services met both the total and RN care minute targets. More than half the sector is non-compliant
- From April 2026, care minutes performance directly affects funding through a new care minutes supplement for metropolitan homes. Miss your minutes, lose your funding
- Care minutes are reported quarterly but funding is paid the following quarter based on the previous quarter's performance. The resident mix changes constantly (deaths, new admissions with different care needs), creating a structural mismatch between what was funded and what needs to be delivered

### Other compliance obligations

- SIRS incident reports: 9 reportable incident types. Priority 1 must be reported within 24 hours. Late notifications are recorded and can trigger compliance reviews, conditions on registration, or civil penalties
- Quality indicators: 14 mandatory indicators reported quarterly. Affects Star Ratings on the My Aged Care website
- Staffing reports: Monthly. Affects Star Ratings and feeds into the Commission's risk scoring
- Quality Standards: 7 Strengthened Standards. Continuous compliance required. Non-compliance can lead to compliance notices, registration suspension, or revocation
- 24/7 RN requirement: At least one registered nurse on-site and on duty at all times at every residential facility

### Why the problem exists

The tools providers currently use were not built for these requirements. Most were designed under the old Act or for general business purposes and have been retrofitted. The result is a fragmented landscape where:

- Data sits in multiple disconnected systems (rostering in one, clinical notes in another, time sheets in a third, compliance tracking in spreadsheets)
- Generating the specific reports the Commission requires means manually pulling data from multiple sources and assembling it
- Senior staff (facility managers, clinical leads, Directors of Nursing) spend hours on compliance administration instead of care oversight
- There is no live visibility into care minutes performance. Providers find out they are behind at the end of the quarter when it is too late to fix

---

## The market

### Size and structure

- The Australian residential aged care sector is worth $38.7 billion (2025, IBISWorld)
- Projected to reach USD $61.3 billion by 2034 at a CAGR of approximately 6.4%
- As of 30 June 2025, there are 636 residential aged care providers operating approximately 2,676 facilities (KPMG 2026)
- Government recurrent expenditure on aged care services was $39.8 billion in 2024-25 (Productivity Commission)
- The aged care workforce includes approximately 549,000 people across all service types (AIHW)
- By 2026, more than 22% of Australians will be aged over 65, up from 16% in 2020

### Consolidation trend

The market is consolidating. 21 providers exited in FY24 (a 3.2% drop in one year). No new residential aged care providers entered in FY25. The top 10 providers now operate 44.7% of all places. Large operators are getting larger through acquisitions (Opal acquired BlueCross; Bolton Clarke acquired Allity and McKenzie brands). Smaller providers are either being acquired or exiting.

This consolidation is the market opportunity. Large operators (Opal, Bolton Clarke, Estia, Regis) have internal compliance teams and IT budgets. They build or buy enterprise solutions. The remaining small-to-mid providers (the majority of the 636 providers) carry identical obligations with a fraction of the resources. They are the underserved segment.

### Regulatory environment

The regulatory shift is permanent. This is not a temporary compliance spike. The Aged Care Act 2024 was passed after a Royal Commission and represents a structural reset of how aged care is governed. The Commission's data-driven supervision model means compliance is no longer about periodic inspections. The regulator is watching continuously through the data providers submit, and cross-referencing it to identify risk before visiting.

---

## Competition

### Competitive landscape (four categories)

**1. Purpose-built for Aged Care Act 2024**

- Statura Care: The most direct competitor. Founded by Paul Benson (MedTech background). Launched early 2026. 35 modules covering residential care and Support at Home. Pricing from $9/bed/month (Compliance Essentials) to $24 (Enterprise). Australian-hosted in Sydney. Currently in targeted rollout with early adopters. No published customer base yet. Strong content marketing (blog posts, comparison guides, buyer's guides). Explicitly positions as "built from the ground up for the Aged Care Act 2024" and frames legacy and generic GRC tools as inferior
- Statura's pricing benchmark is important: $9-$24/bed/month means a 100-bed facility pays $900-$2,400/month ($10,800-$28,800/year). Volume discounts start at 200 beds

**2. Legacy clinical platforms**

- Telstra Health, Leecare, iCare: Established customer bases, deep clinical documentation features. Built under the old 8 Quality Standards and previous regulatory framework. Slow to update to the new 7 Strengthened Standards, expanded SIRS, new responsible persons register, and care minutes supplement. Dated user interfaces. Strong in clinical workflow but compliance is bolted on, not native

**3. Generic GRC platforms adapted for aged care**

- Protecht: 20+ year old Australian enterprise governance, risk, and compliance platform. Feature-rich for general risk management (risk registers, audit workflows, policy management). Thin on aged care-specific regulatory knowledge. Heavy configuration required. High cost. Steep learning curve for aged care staff

**4. International platforms adapted for Australia**

- AlayaCare (Canada), Person Centred Software (UK): Mature platforms with large development teams. Australian regulatory compliance is a secondary priority. Updates to reflect local legislation changes consistently lag. Designed for their home markets and adapted

### The actual gap (validated in interviews)

The problem is not that no tools exist. It is that:

- Existing tools do not integrate with each other. Providers run 50-60 disconnected tools (Jimmy Muzzell's estimate at LifeView)
- None of the current tools are designed around the specific reporting deadlines and workflows of the new Act
- Generating the outputs the regulator requires still involves significant manual work
- Providers are resorting to custom-built dashboards (LifeView is building one with Bluevine) because nothing off the shelf connects their systems
- Most providers are risk-averse about new technology. Jimmy described reaching out to other providers about an AI trial and being told "not touching that"

---

## Primary research (interviews conducted April 2026)

### Interview 1: Jimmy Muzzell, Chief Operating Officer, LifeView

- Context: 5 residential aged care facilities, 283 residents total
- How we got the interview: Walked in unannounced. Receptionist connected us. 40-minute conversation. He gave us his business card on the way out
- On care minutes: Confirmed it is the single biggest operational headache. His facilities must deliver 217 minutes per resident per day (case mix adjusted). Care minutes reported quarterly, paid the following quarter. Resident mix changes constantly (deaths, new admissions). When they miss targets, they over-staff to catch up, with extra staff "standing around doing nothing." Casuals cost 30-35% more than permanent staff. Agencies cost even more
- On tools: Estimated "50 or 60 tools" in the space. None talk to each other. Different departments within the same compliance framework ask for the same data separately because their systems are not connected. His exact words: "Someone just needs to be able to come up with this. They all don't talk."
- On audits: Uses HumanForce for rostering and time sheets. The finance team overseas can run reports in 5-10 minutes, but assembling the audit response the Commission wants takes 2+ hours per request
- On technology adoption: LifeView is working with Bluevine to build a data dashboard (similar to Jomo, a US platform) that will API into payroll, complaints, and feedback. Also trialling an AI tool for clinical documentation that compares progress notes against policies and generates summaries for the lead nurse. Before the AI tool, lead nurses sat at computers for hours every shift reviewing notes manually. Jimmy said most providers are scared of new tech. He reached out to other providers to join the trial and several refused
- On the regulator: Confirmed the trigger system. Miss care minutes by more than 5% for two quarters and the Commission comes to you. Then they match the shortfall against clinical documentation to look for evidence that care quality is also failing

### Interview 2: Kerry [Surname], CEO, Phillips Institute

- Context: Private registered training organisation (RTO) that trains Personal Care Assistants (PCAs). Contractual arrangements with at least 15 aged care organisations for student placements
- How we got the interview: Approached reception. Secretary fetched the CEO directly
- On regulatory changes: Confirmed changes affect both training providers and the facilities they partner with. When standards change, Phillips must update learning resources, assessments, and student guidance. Their continuous improvement system is largely manual (staff log changes into a centralised system but identification depends on individuals noticing things)
- On care minutes: Immediate and direct response. Her exact words: "It's huge." Raised unintended consequences: care minutes try to quantify personal care that does not naturally break into measurable time blocks. Noted it affects both quality of care and people's jobs
- On the training gap: Phillips is not currently teaching students how to log care minutes on the job because it sits outside the current curriculum, even though graduates will be required to do it on the floor
- Offered to connect us with her trainer network. Specifically recommended Jasmine (available Wednesdays) who has the deepest knowledge of conditions in facilities. Also introduced us to Anita, a recently graduated PCA

### Interview 3: Anita [Surname], Personal Care Assistant

- Context: Recently completed Certificate III in Individual Support. Just finished placement at a residential aged care facility
- On PCA documentation: Mostly checkbox-based on a computer system. Facility sets up a list of residents per PCA. Tick off activities (showering, meals, moisturiser, hearing aids, dental care). Routine paperwork takes 30-40 minutes per shift
- On progress notes: Written for non-routine events. 30-50 words. If a resident is aggressive or refuses care, write a short note
- On risk identification: Comes from the care plan, created by RNs and management at admission. PCAs follow the plan and ask the nurse if unsure. High fall risk and two-person assist requirements are flagged in the care plan
- Key insight: The documentation burden at PCA level is manageable. The pain is upstream at the registered nurse and management level, where data must be reviewed, cross-referenced against policies and procedures, and turned into compliance reports

### Patterns from primary research

1. Care minutes are the immediate, universal pain point. Every person we spoke to raised it without prompting
2. The tool landscape is fragmented. Providers run multiple disconnected systems and fill gaps with manual processes. No single platform connects everything
3. The compliance burden falls on middle management and clinical leads, not frontline PCAs. The product must serve facility managers, Directors of Nursing, and clinical leads

---

## The team

Three co-founders, all enrolled at Monash University, participating in the FastTrack accelerator program.

- [Name]: Computer Science. Technical build, product development, system architecture. Works at NNC (cable and logistics company) where manual compliance reporting and disconnected systems are the daily reality. This firsthand exposure to operational compliance pain in B2B is what led the team to aged care
- [Name]: Entrepreneurship and business. Sales outreach, stakeholder interviews, go-to-market strategy
- [Name]: Finance and law. Financial modelling, regulatory analysis, pricing strategy

---

## Product scope (current plan)

### What Carelog does (MVP)

- Care minutes tracking: Connect to rostering/time sheet systems (starting with HumanForce). Calculate care minutes per resident per day against the facility's AN-ACC adjusted target. Show live performance, not end-of-quarter surprises
- SIRS incident tracking: Log incidents, auto-calculate Priority 1 (24-hour) and Priority 2 deadlines, countdown alerts, notification templates ready to submit through the My Aged Care portal
- Quarterly report generation: Produce the care minutes data in the format the Quarterly Financial Report requires. Reduce audit response time from hours to minutes

### What Carelog does not do

- Clinical documentation or electronic health records
- Rostering or time sheet management (it reads from those systems, it does not replace them)
- Clinical decision support or AI-driven care predictions (this would trigger TGA medical device classification)
- Payroll or billing
- Anything that replaces clinical judgment

### Positioning

Carelog is a compliance layer, not a clinical platform. It sits on top of existing systems and turns the data they already hold into the specific outputs the regulator requires. It does not ask providers to rip and replace anything. It connects to what they have and fills the gap between operational data and regulatory reporting.

---

## Pricing reference

Based on Statura Care's published pricing (the closest competitor):

- Compliance Essentials: $9/bed/month
- Professional: $14/bed/month
- Professional + Clinical: $19/bed/month
- Enterprise: $24/bed/month
- Volume discounts start at 200 beds (5% at 100-250, 10% at 250-500, 15% at 500+)
- Implementation and onboarding: $3,000-$10,000 (vendor-led)
- Data migration: $2,000-$5,000

Carelog's pricing has not been set. Testing willingness to pay ($10-$15/bed/month for automated care minutes reporting and SIRS tracking) is an immediate next step.

---

## Key sources and references

- Aged Care Act 2024: https://www.legislation.gov.au/C2024A00104/latest/text
- Aged Care Quality and Safety Commission: https://www.agedcarequality.gov.au/understanding-new-aged-care-act
- Care minutes requirements: https://www.health.gov.au/our-work/care-minutes-registered-nurses-aged-care/care-minutes
- Care minutes enforcement: https://www.agedcarequality.gov.au/news-publications/media-releases/commission-cracks-down-aged-care-providers-failing-meet-care-minutes
- SIRS overview: https://www.health.gov.au/our-work/serious-incident-response-scheme-sirs
- SIRS reporting: https://www.agedcarequality.gov.au/workers/reporting-incidents
- Productivity Commission RoGS 2026: https://www.pc.gov.au/ongoing/report-on-government-services/community-services/aged-care-services/
- KPMG 2026 aged care market analysis: https://kpmg.com/au/en/insights/industry/aged-care-market-analysis.html
- IBISWorld market size: https://www.ibisworld.com/australia/industry/aged-care-residential-services/5531/
- AIHW aged care data: https://www.aihw.gov.au/reports/australias-welfare/aged-care
- IMARC market projections: https://www.imarcgroup.com/australia-aged-care-market
- Sector consolidation data: https://agedcareonline.com.au/2025/11/Australia-s-Top-10-Largest-Residential-Aged-Care-Providers-in-2025
- Statura Care (primary competitor): https://www.statura.care/
- Statura Care pricing: https://statura.care/blog/aged-care-compliance-software-australia-guide
- Statura Care launch coverage: https://insideageing.com.au/compliance-first-platform-launches-to-support-aged-care-reform/
- Statura Care competitor comparison: https://statura.care/blog/best-aged-care-compliance-software-australia
- Reporting requirements: https://www.health.gov.au/our-work/residential-aged-care/managing/reporting

---

## Communication style guidelines

When writing about Carelog or representing this product in any context:

- No jargon. No buzzwords. No inflated promises
- Never say "revolutionary," "game-changing," "cutting-edge," "leverage," "synergy," "empower," or "unlock"
- Never use em dashes
- Do not promise things the product does not do yet
- Be specific. Name the regulation, the deadline, the number. "215 minutes" not "mandatory care targets"
- The tone is professional and direct. Not corporate, not startup-casual. The audience is facility managers and Directors of Nursing who are tired of being sold to
- If a claim cannot be backed by a specific source, do not make it
- Do not describe the product as AI-powered unless it literally uses AI for a specific function. The MVP is data integration and report generation, not artificial intelligence
