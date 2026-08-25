"""Pack 05: HGB and German accounting for a Frankfurt or Munich interview.

Every accounting question in the bank is framed in US GAAP with a handful of
IFRS contrasts. A German mid-market target reports under HGB, and the first
thing an analyst does on a Mittelstand mandate is bridge it. Nothing in the
corpus mentions HGB once.

Answers are written so the German term and the English explanation both
appear, because these get asked in German and answered in German.

Tax thresholds and rates change. Re-check the interest barrier allowance and
the trade tax multiplier before quoting a number in a room.
"""
import json
from pathlib import Path

Q = []
def q(text, ans, rubric, *, topic="accounting", d=3, tags=(), trap=None, sub=None):
    Q.append({"q": text, "a": ans, "rubric": list(rubric), "mistakes": [trap] if trap else [],
              "topic": topic, "difficulty": d, "subtopic": sub,
              "tags": sorted(set(list(tags) + ["german-accounting"])),
              "locator": f"DE{len(Q)+1:02d}"})

q("Why can a company build hidden reserves under HGB but not under IFRS?",
  "Because the two frameworks are answering different questions. HGB exists primarily to protect "
  "creditors and to determine what may be distributed; IFRS exists to inform investors about "
  "economic reality. Once you have that, everything else follows.\n\n"
  "HGB is governed by the Vorsichtsprinzip, the prudence principle, expressed through two rules. The "
  "Realisationsprinzip: unrealised gains may not be recognised. And the Imparitatsprinzip: "
  "unrealised losses must be recognised. Assets are carried at the lower of cost and market, the "
  "Niederstwertprinzip, and liabilities at the higher of the possible values. Crucially, historical "
  "cost is a ceiling: an asset that has risen in value stays at cost, and even after a write-down "
  "and a recovery you may only write back up to original cost.\n\n"
  "Stille Reserven, hidden reserves, are the gap between an asset's carrying amount and its true "
  "value. They arise from that ceiling, from depreciating faster than economic wear, and from "
  "provisions measured conservatively. They are hidden because the balance sheet does not show them.\n\n"
  "IFRS eliminates most of them deliberately. Fair value through profit or loss or through other "
  "comprehensive income for financial instruments, the revaluation model for property plant and "
  "equipment, investment property at fair value, and impairment reversals where the reason for the "
  "impairment has gone. The stated objective is faithful representation, and a systematic bias "
  "towards understatement is a misrepresentation even if it is a conservative one.\n\n"
  "For a valuation that matters concretely: HGB equity systematically understates the economic net "
  "asset position, so book value is a weaker guide, and the real estate on a Mittelstand balance "
  "sheet bought in 1985 is worth a multiple of its carrying amount.",
  ["Identifies the underlying purpose difference: creditor protection and distributable profit versus investor information",
   "Names the Vorsichtsprinzip and both the Realisationsprinzip and the Imparitatsprinzip",
   "Explains historical cost as a ceiling, so unrealised gains cannot be recognised",
   "Names the sources of hidden reserves: the cost ceiling, accelerated depreciation, conservative provisions",
   "Draws the valuation consequence: HGB equity understates the economic position, especially on old real estate"],
  d=4, trap="Explaining prudence as 'German conservatism'. It is a legal consequence of HGB determining what may be distributed to shareholders.")

q("Which German companies report under HGB and which under IFRS?",
  "It depends on whether the statements are single entity or consolidated, and on whether the "
  "company is capital-market oriented.\n\n"
  "The Einzelabschluss, the single-entity statutory accounts of a German company, must be prepared "
  "under HGB. Always, without exception. That is the document that determines what may be "
  "distributed as a dividend and it is the starting point for the tax computation.\n\n"
  "The Konzernabschluss, the consolidated accounts, is where the choice sits. Under the EU IAS "
  "Regulation, companies whose securities are admitted to trading on a regulated market in the EU "
  "must prepare consolidated accounts under IFRS as adopted by the EU. Companies that are not "
  "capital-market oriented may prepare consolidated HGB accounts instead, and most Mittelstand "
  "groups do, because it is cheaper and because their lenders are German banks who read HGB "
  "comfortably. They may opt into IFRS voluntarily, and some do when they start borrowing "
  "internationally or preparing for a sale.\n\n"
  "Two practical consequences for a banker. On a Mittelstand target you will very often be given "
  "HGB consolidated accounts, or in a smaller group only the parent's Einzelabschluss plus "
  "unaudited management consolidations, which is a diligence problem. And a listed German group "
  "reports IFRS at group level while every subsidiary still keeps HGB books underneath, so the two "
  "frameworks coexist inside the same company.",
  ["States the Einzelabschluss must always be HGB and explains what it is used for",
   "States the EU IAS Regulation requires IFRS consolidated accounts for companies on a regulated market",
   "Notes non-capital-market-oriented groups may use HGB consolidated accounts and usually do",
   "Draws the practical consequences for a Mittelstand target and for a listed group's subsidiaries"],
  d=3, trap="Saying German companies 'use HGB' or 'use IFRS'. Most use both, at different levels of the group.")

q("How is goodwill treated under HGB compared to IFRS, and what does it do to a multiple?",
  "Under IFRS, acquired goodwill is capitalised and not amortised. It is tested for impairment at "
  "least annually, and an impairment cannot be reversed. So goodwill sits on the balance sheet "
  "indefinitely and there is no recurring charge against earnings.\n\n"
  "Under HGB, goodwill acquired in a business combination must be capitalised and must be amortised "
  "over its expected useful life. Where that life cannot be estimated reliably, HGB requires "
  "amortisation over ten years, and the notes must explain the period used. So there is a real, "
  "recurring, non-cash charge running through the P&L.\n\n"
  "The consequence for comparison is that HGB EBIT and net income are systematically lower than the "
  "IFRS equivalents for an acquisitive company, because the goodwill amortisation sits above EBIT. "
  "EBITDA is unaffected, since amortisation is added back.\n\n"
  "So if you are applying an EV/EBIT multiple derived from listed IFRS comparables to an HGB "
  "Mittelstand target that has made acquisitions, you will undervalue it, potentially badly. The "
  "answer is either to adjust the HGB EBIT by adding back goodwill amortisation, which is what a "
  "buyer will do in its own model, or to work off EBITDA, which is one reason EBITDA multiples are "
  "used so heavily in the German mid-market despite the well-known objections to them.",
  ["States IFRS capitalises goodwill with impairment testing only, no amortisation, no reversal",
   "States HGB requires capitalisation and amortisation, with a ten-year default where the life is not reliably estimable",
   "Explains that HGB EBIT and net income are lower while EBITDA is unaffected",
   "Draws the valuation consequence for applying an IFRS-derived EBIT multiple to an HGB target, and names the adjustment"],
  d=4, trap="Assuming the difference washes out. It does not: it sits above EBIT, so every EBIT-based multiple is distorted.")

q("Why does a German pension provision differ between HGB and IFRS?",
  "Mostly because of the discount rate, and the difference has been large.\n\n"
  "Under IAS 19, the defined benefit obligation is discounted at the yield on high quality corporate "
  "bonds of matching currency and duration at the balance sheet date. It is a spot rate, so the "
  "liability moves with the market, and remeasurements go through other comprehensive income rather "
  "than profit or loss.\n\n"
  "Under HGB, pension obligations are discounted at an average market rate over a trailing period, "
  "published monthly by the Bundesbank, with the averaging period for pension obligations "
  "specifically extended to ten years. That is a deliberate smoothing device, introduced because "
  "using a spot rate would have made German balance sheets swing violently through the low rate "
  "period. HGB also permits a simplifying assumption of a fifteen-year residual term.\n\n"
  "The effect is directional and predictable. When market rates are below the trailing average, "
  "which was the case for most of the 2010s, the HGB rate is higher than the IAS 19 rate, so the "
  "HGB liability is smaller, sometimes very substantially so. When rates rise sharply, as they did "
  "recently, the relationship inverts for a period until the average catches up.\n\n"
  "For a banker that means two things. Do not take the HGB pension number as the economic liability "
  "when computing the debt-like items in an EV bridge; ask for the IFRS or actuarial figure. And "
  "German pension obligations are frequently unfunded, sitting on the balance sheet as a provision "
  "with no matching plan assets, which is a genuine cash obligation and a genuine debt-like item, "
  "unlike a UK scheme where there are usually assets against it.",
  ["States IAS 19 uses a spot high quality corporate bond rate at the balance sheet date",
   "States HGB uses a trailing average rate published by the Bundesbank, over ten years for pensions",
   "Explains the direction of the difference and why it inverts when rates move sharply",
   "Draws the practical instruction: use the actuarial or IFRS figure for the EV bridge",
   "Notes German pension obligations are frequently unfunded, unlike UK schemes"],
  d=4, trap="Treating the HGB provision as the economic liability. It is smoothed by construction, not measured.")

q("How does HGB treat long-term contracts, and why does that matter for a Mittelstand engineering business?",
  "HGB does not permit percentage of completion in the way IFRS does. The Realisationsprinzip means "
  "revenue is realised on delivery or on transfer of risk, so a multi-year contract is essentially "
  "accounted for on a completed contract basis, with work in progress carried at cost and any "
  "expected loss on the contract provided for immediately under the Imparitatsprinzip.\n\n"
  "IFRS 15 requires revenue to be recognised over time where the customer controls the asset as it "
  "is created or the entity has an enforceable right to payment for work completed to date, which "
  "for most engineering and construction contracts means a percentage of completion approach.\n\n"
  "So the same business shows profoundly different accounts. Under HGB, revenue and profit are lumpy "
  "and back-loaded, appearing in the year of completion, while costs accumulate in inventory. Under "
  "IFRS, both are smoothed across the contract life.\n\n"
  "That matters concretely on a German industrial or engineering mandate. LTM EBITDA off HGB "
  "accounts may be meaningless, because it reflects which contracts happened to complete in that "
  "window rather than the underlying activity. The multiple you apply to it is therefore wrong in "
  "an unpredictable direction. The right response is to ask for the contract-by-contract position "
  "and rebuild the earnings on a percentage of completion basis, which is exactly what a financial "
  "due diligence provider will do, and it is often the single most valuable analysis in the "
  "diligence report.",
  ["States HGB requires realisation on delivery, so effectively completed contract accounting",
   "States IFRS 15 requires over-time recognition where the criteria are met, effectively percentage of completion",
   "Explains that HGB earnings are lumpy and back-loaded while IFRS smooths them",
   "Draws the practical consequence: LTM EBITDA off HGB accounts may not reflect underlying activity, and must be rebuilt"],
  d=4, trap="Applying an LTM multiple to HGB earnings from a long-contract business without checking which contracts completed.")

q("What is the Massgeblichkeitsprinzip, and what is the difference between the Handelsbilanz and the Steuerbilanz?",
  "The Handelsbilanz is the commercial balance sheet prepared under HGB. The Steuerbilanz is the tax "
  "balance sheet prepared under the Einkommensteuergesetz for the purpose of computing taxable "
  "profit. They are separate documents that start from the same place.\n\n"
  "The Massgeblichkeitsprinzip, the authoritativeness principle, is the rule that the commercial "
  "balance sheet is authoritative for the tax balance sheet: the tax computation starts from HGB "
  "accounting and departs from it only where tax law requires. That is a fundamentally different "
  "architecture from the UK or the US, where the accounts and the tax computation are conceptually "
  "independent and reconciled.\n\n"
  "Since the accounting modernisation reform, the reverse principle, umgekehrte Massgeblichkeit, "
  "under which tax-driven treatments had to be mirrored in the commercial accounts, was abolished. "
  "The result is that the two have drifted further apart and deferred taxes have become more "
  "meaningful under HGB than they used to be.\n\n"
  "The practical divergences to know: provisions for expected losses on onerous contracts are "
  "required under HGB and not deductible for tax; the capitalisation option for self-created "
  "intangibles under HGB does not carry to tax, where a prohibition applies; and depreciation "
  "methods and useful lives differ, since tax uses prescribed tables.\n\n"
  "Why a banker cares: the effective tax rate in a German company's accounts is not simply the "
  "statutory rate, and reconciling it requires knowing which of these differences are permanent and "
  "which are timing.",
  ["Defines the Handelsbilanz and the Steuerbilanz and states they are separate documents",
   "States the Massgeblichkeitsprinzip: the commercial balance sheet is authoritative for the tax computation",
   "Contrasts this architecture with the independent-and-reconciled approach in the UK and US",
   "Names concrete divergences such as onerous contract provisions and self-created intangibles",
   "Draws the consequence for reconciling an effective tax rate"],
  d=4, trap="Assuming HGB profit is taxable profit. Authoritativeness means it is the starting point, not the answer.")

q("How does Gewerbesteuer work, and how does it change a German DCF?",
  "Gewerbesteuer is a municipal trade tax on business income, and it sits alongside corporation tax "
  "rather than replacing it. The mechanics: a uniform federal base rate is applied to trade income "
  "to give a base amount, and each municipality then applies its own multiplier, the Hebesatz, which "
  "it sets itself. Effective rates therefore vary materially by location, from around seven percent "
  "in low-tax municipalities to the high teens in the major cities, with a typical rate in the "
  "middle teens.\n\n"
  "So the total corporate tax burden in Germany is corporation tax at fifteen percent plus the "
  "solidarity surcharge on it, giving roughly 15.8 percent, plus Gewerbesteuer, which brings the "
  "combined rate to somewhere around thirty percent depending on where the company sits.\n\n"
  "The feature that matters for a DCF is the add-back. Trade income is not simply taxable income: "
  "certain financing costs are added back to the base, currently a quarter of the sum of interest "
  "expense and the financing components deemed to be embedded in rents, leases and licence fees, "
  "with an allowance applied before the add-back. The consequence is that interest is only partly "
  "deductible for trade tax purposes, so the tax shield on debt in Germany is smaller than the "
  "combined statutory rate implies.\n\n"
  "For a DCF that means two things. Use a location-specific effective rate rather than a national "
  "average, because a Munich business and a business in a low-Hebesatz municipality genuinely differ. "
  "And if you are computing WACC with a tax shield, the effective shield on interest is below the "
  "full combined rate because of the add-back, which is a real and often-missed adjustment.",
  ["Describes the mechanism: a federal base rate times a municipal Hebesatz, so the rate varies by location",
   "Gives the combined burden as corporation tax plus solidarity surcharge plus trade tax, around thirty percent",
   "Names the financing add-back to the trade tax base and its effect on interest deductibility",
   "Draws two DCF consequences: use a location-specific rate, and reduce the assumed tax shield on debt"],
  d=4, trap="Using a single German tax rate. The trade tax multiplier is set by the municipality and the interest add-back reduces the debt shield.")

q("What is the Zinsschranke, and why does it constrain a German LBO?",
  "The interest barrier limits the deductibility of net interest expense to a percentage of tax "
  "EBITDA, currently thirty percent, with disallowed interest carried forward. There is a safe "
  "harbour: net interest expense below a threshold, in the order of three million euros, escapes the "
  "restriction entirely, and there are escape clauses based on group equity ratio comparison and for "
  "companies not part of a group, though those have been narrowed over time.\n\n"
  "It constrains a German LBO directly. A sponsor structuring a highly leveraged acquisition of a "
  "German target expects the interest to shelter the operating profit, and the barrier caps that "
  "shelter at thirty percent of EBITDA. Above the safe harbour, interest beyond that limit produces "
  "no current tax benefit, so the after-tax cost of the marginal euro of debt rises sharply and the "
  "case for leveraging further weakens.\n\n"
  "That is one reason German acquisition structures pay so much attention to the tax group. An "
  "Organschaft between the acquisition vehicle and the target consolidates their tax positions, so "
  "the interest at the bidco can be set against the target's operating profit rather than sitting "
  "stranded in a holding company with no income. Without it, the interest deduction can be "
  "economically worthless.\n\n"
  "The rules have been amended repeatedly, and the interaction with the EU anti tax avoidance "
  "directive has moved the detail, so check the current thresholds and escape clauses before relying "
  "on a number.",
  ["States the rule: net interest deductible only up to thirty percent of tax EBITDA, with carryforward",
   "Names the safe harbour threshold and the equity ratio escape clause",
   "Explains the effect on an LBO: the marginal euro of debt loses its shield above the limit",
   "Connects it to the need for an Organschaft so interest meets the target's operating profit",
   "Flags that the thresholds have changed repeatedly and should be checked"],
  d=5, trap="Assuming a German LBO gets the same interest shield as a UK or US one. The barrier caps it at thirty percent of EBITDA.")

q("What is an Organschaft and why does a bidder want one?",
  "A German tax consolidation. It allows the profits and losses of a controlled company, the "
  "Organgesellschaft, to be attributed to a controlling company, the Organtrager, so the group is "
  "taxed on the net position rather than each entity separately.\n\n"
  "The requirements are strict. Financial integration: the controlling company must hold the "
  "majority of the voting rights from the beginning of the subsidiary's financial year. And, "
  "critically, a profit and loss transfer agreement, a Gewinnabfuhrungsvertrag, must be in place and "
  "must be concluded for a minimum term of five years and actually performed throughout. If it is "
  "terminated early without good cause, or not properly executed, the Organschaft can fail "
  "retroactively for the whole period, which is a serious tax exposure.\n\n"
  "A bidder wants one for the reason above: the acquisition financing interest sits at the bidco, "
  "which has no operating income, while the profit is at the target. Without consolidation the "
  "interest deduction is stranded. With it, the interest offsets the target's operating profit and "
  "the shield is real.\n\n"
  "The link to the public M&A side is worth making. For a listed AG target, the profit and loss "
  "transfer agreement needed for the Organschaft is the same instrument as the domination and "
  "profit and loss transfer agreement that a bidder needs to control cash flow, requires the same "
  "seventy-five percent shareholder approval, and triggers the same minority compensation and "
  "appraisal exposure. So the tax reason and the cash control reason point at the same document.",
  ["Defines the Organschaft as tax consolidation attributing profits and losses to the controlling entity",
   "Names the requirements: majority voting rights from the start of the year, and a profit and loss transfer agreement for a minimum five-year term",
   "Notes retroactive failure if the agreement is broken early",
   "Explains the bidder's motive: unstranding acquisition interest against the target's operating profit",
   "Links it to the DPLTA required for a listed AG and the associated minority compensation"],
  d=5, trap="Thinking a majority stake creates a tax group. Germany requires the profit and loss transfer agreement as well.")

q("How does HGB treat leases, and what does that do to an EV comparison?",
  "HGB has no equivalent of IFRS 16. Lease classification follows the tax guidance, and an operating "
  "lease stays entirely off balance sheet: the rent is an operating expense and there is no right of "
  "use asset and no lease liability. Only finance leases, where the lessee is treated as the "
  "economic owner, are capitalised.\n\n"
  "IFRS 16 abolished that distinction for lessees. Almost all leases are capitalised, producing a "
  "right of use asset and a lease liability, and the charge splits into depreciation of the asset "
  "and interest on the liability, both of which sit below EBITDA. So the same company under IFRS "
  "reports higher EBITDA, higher debt and a higher asset base than under HGB.\n\n"
  "The comparison problem is immediate. If you value an HGB-reporting target on an EV/EBITDA multiple "
  "taken from IFRS-reporting listed comparables, you are applying a post-IFRS 16 multiple, which is "
  "structurally lower because the denominator is inflated, to a pre-IFRS 16 EBITDA. You will "
  "systematically undervalue the target, and the error is larger the more lease-intensive the "
  "business is, so it is worst exactly where it matters most: retail, logistics, transport and "
  "hospitality.\n\n"
  "The two fixes are to capitalise the target's operating leases yourself and add the liability to "
  "net debt, or to strip the lease effect out of the comparables and work on a pre-IFRS 16 basis "
  "throughout. Either is defensible; mixing them is not, and mixing them is the common error.",
  ["States HGB keeps operating leases off balance sheet with rent as an operating expense",
   "States IFRS 16 capitalises almost all leases, raising EBITDA, debt and assets",
   "Explains the resulting mismatch when applying an IFRS-derived multiple to an HGB EBITDA",
   "Notes the error scales with lease intensity, naming affected sectors",
   "Gives both fixes and warns against mixing bases"],
  d=4, trap="Comparing an HGB EBITDA to an IFRS 16 comparable multiple without adjusting either side.")

q("What size classes does HGB use, and why does it matter in diligence?",
  "HGB classifies companies as micro, small, medium or large by reference to three criteria, balance "
  "sheet total, revenue and average employees, with a company falling into a class if it exceeds two "
  "of the three thresholds on two consecutive balance sheet dates.\n\n"
  "The classification drives real reliefs. Small companies face reduced disclosure in the notes, are "
  "generally exempt from statutory audit, and file an abbreviated balance sheet with no profit and "
  "loss account in the Bundesanzeiger. Medium-sized companies get partial reliefs, including "
  "abbreviated profit and loss presentation. Only large companies file the full set.\n\n"
  "That matters in diligence because it determines what exists before you ask for it. On a small "
  "German target you may be dealing with unaudited accounts, no published profit and loss account, "
  "and notes that omit the segment, related party and commitment disclosures you would rely on "
  "elsewhere. The published filing tells you almost nothing about profitability.\n\n"
  "The practical consequences: assume audited accounts have to be requested rather than assumed, "
  "expect to work from management accounts whose basis needs to be established, budget for a "
  "financial due diligence scope that includes verifying rather than only analysing, and treat the "
  "absence of an audit as a reason for a buyer to want completion accounts rather than a locked box.",
  ["Names the three criteria and the two-of-three-over-two-years test",
   "States the reliefs: audit exemption and abbreviated filing for small companies, partial reliefs for medium",
   "Explains that the published filing may contain no profit and loss account at all",
   "Draws diligence consequences, including the effect on the completion mechanism choice"],
  d=3, trap="Assuming a German target has audited accounts. Below the size thresholds it very often does not.")

q("Under HGB, when may a company capitalise development costs, and what happens if it does?",
  "HGB gives an option. Self-created intangible fixed assets may be capitalised, but the option "
  "excludes brands, mastheads, publishing titles, customer lists and comparable items, and only the "
  "development phase qualifies; research costs may never be capitalised, and where the two phases "
  "cannot be reliably separated, neither may be.\n\n"
  "IFRS is not optional. IAS 38 requires capitalisation once the criteria are met, including "
  "technical feasibility, intention and ability to complete and use or sell, probable future "
  "economic benefits and reliable measurement of the cost.\n\n"
  "Two consequences follow from the HGB option. First, comparability breaks down: two otherwise "
  "identical German companies can report materially different EBIT depending on a policy choice, so "
  "you have to read the accounting policies note before you compare them, and before you apply a "
  "multiple to either.\n\n"
  "Second, and this is the distinctively German part, capitalisation triggers a distribution "
  "restriction. Amounts capitalised for self-created intangibles, net of related deferred tax, are "
  "blocked from distribution: the company must have freely available reserves at least equal to that "
  "amount before it can pay a dividend. That is the creditor protection logic showing through. A "
  "company that capitalises heavily is restricting its own ability to distribute, which is a "
  "genuinely relevant fact if you are looking at how cash gets upstreamed after an acquisition.",
  ["States HGB gives an option for self-created intangibles and names the exclusions and the research prohibition",
   "States IAS 38 makes capitalisation mandatory once the criteria are met",
   "Explains that the option destroys comparability between German companies and requires reading the policy note",
   "Names the distribution blocking consequence and connects it to creditor protection and to upstreaming cash"],
  d=4, trap="Missing the distribution block. It is the point that shows you understand why HGB is written the way it is.")

q("What is the difference between a GmbH and an AG, and why does it matter in a transaction?",
  "The GmbH is the private limited company: the standard vehicle for Mittelstand businesses, "
  "requiring a minimum share capital of twenty-five thousand euros, managed by one or more "
  "Geschaftsfuhrer who are bound by shareholder instructions, with a supervisory board only where "
  "co-determination thresholds require one. Shares are not securities and are not freely "
  "transferable in a market.\n\n"
  "The AG is the stock corporation: minimum share capital of fifty thousand euros, mandatory "
  "two-tier board with a Vorstand that manages on its own responsibility under section 76 AktG and "
  "an Aufsichtsrat that appoints and supervises it, and shares that are securities capable of being "
  "listed.\n\n"
  "The transaction consequences are practical. Governance: a GmbH's managing directors take "
  "instructions from the shareholders, so a majority buyer controls the business directly, whereas "
  "an AG's management board does not, which is why controlling an AG requires the domination "
  "agreement discussed elsewhere. Transfer formality: a transfer of GmbH shares must be notarised by "
  "a German notary, which is a real logistical step on a signing, and it is one reason German "
  "closings are choreographed around a notary's diary. And flexibility: a GmbH's articles can be "
  "shaped far more freely, whereas the AktG is largely mandatory law, which is why sponsors "
  "frequently convert an AG target into a GmbH or a GmbH & Co. KG after acquisition.\n\n"
  "The GmbH & Co. KG is worth knowing too: a limited partnership whose general partner is a GmbH, "
  "combining limited liability with partnership tax transparency, and extremely common in "
  "family-owned German businesses.",
  ["Distinguishes the two on share capital, governance structure and whether shares are securities",
   "States the AG management board manages on its own responsibility while GmbH managing directors take shareholder instructions",
   "Names the notarisation requirement for GmbH share transfers and its effect on closing logistics",
   "Notes the AktG is largely mandatory law, which is why sponsors convert AG targets post-acquisition",
   "Names the GmbH & Co. KG and why it is common in family businesses"],
  topic="deal_process", tags=["german-public-m-a"], d=3,
  trap="Treating the two as German versions of Ltd and PLC. The governance difference is substantive, not cosmetic.")

q("A German target reports HGB. Walk me through bridging it to something you can value.",
  "Six adjustments, in the order I would do them.\n\n"
  "Leases. HGB keeps operating leases off balance sheet, so if the comparables are IFRS 16, "
  "capitalise the target's leases and add the liability to net debt, or strip the effect out of the "
  "comparables. Pick one basis and hold it.\n\n"
  "Goodwill amortisation. Add it back to EBIT, because the IFRS comparables do not carry it. EBITDA "
  "is unaffected.\n\n"
  "Long-term contracts. If the business has multi-year contracts, rebuild revenue and earnings on a "
  "percentage of completion basis, because HGB's completion-based recognition makes LTM earnings a "
  "function of which contracts finished rather than of activity.\n\n"
  "Pensions. Replace the smoothed HGB provision with the actuarial or IAS 19 figure, and treat "
  "unfunded obligations as a debt-like item in the bridge, net of any deferred tax asset.\n\n"
  "Hidden reserves. Identify assets carried far below value, above all real estate bought decades "
  "ago, and decide whether they are operating assets you are valuing through the multiple or surplus "
  "assets you should value separately and add to enterprise value. In a Mittelstand business the "
  "operating property is very often held in a separate entity owned by the family and let to the "
  "company, in which case the rent needs normalising to market as well.\n\n"
  "Owner-related items. Founder remuneration above or below market, family members on the payroll, "
  "private cars and travel, and related-party arrangements at non-market terms all have to be "
  "normalised, and this is usually the largest single adjustment on a founder-owned target.\n\n"
  "Then check the resulting effective tax rate against a location-specific German rate, rather than "
  "carrying whatever the accounts show.",
  ["Names the lease adjustment and insists on a consistent basis with the comparables",
   "Names goodwill amortisation add-back to EBIT",
   "Names rebuilding long-term contract revenue on a percentage of completion basis",
   "Names replacing the smoothed HGB pension provision and treating unfunded obligations as debt-like",
   "Names hidden reserves and the separately-held family real estate, with rent normalisation",
   "Names owner remuneration and related-party normalisation as usually the largest adjustment"],
  d=5, trap="Adjusting for one or two of these. The bridge only works if you do all of them and keep the comparables on the same basis.")

json.dump({
  "title": "HGB and German accounting for a Frankfurt mandate",
  "origin": "self_authored",
  "status": "active",
  "note": ("Authored: no source in the corpus mentions HGB. Tax thresholds -- the "
           "interest barrier allowance, the trade tax multiplier, the corporation tax "
           "rate -- move with legislation. Verify before quoting a figure."),
  "items": Q,
}, open(Path(__file__).with_name("05-hgb-dach.json"), "w"), indent=1, ensure_ascii=False)
print(f"{len(Q)} items")
