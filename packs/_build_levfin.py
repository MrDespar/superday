"""Pack 04: leveraged finance and European private credit.

The bank had a debt schedule, a revolver and a dividend recap, and nothing on
unitranche or direct lending -- which is where mid-market European sponsor
financing actually happens now, and which was the subject of the candidate's
own bachelor thesis.

Grounded in Wall Street Prep's leveraged finance guide and its interview
guide's Debt & Leveraged Finance section, with the benchmark rates brought
forward from LIBOR to SOFR and Euribor and the private credit material added,
since neither source covers it.
"""
import json
from pathlib import Path

Q = []
def q(text, ans, rubric, *, topic="dcm", d=3, tags=(), trap=None, sub=None):
    Q.append({"q": text, "a": ans, "rubric": list(rubric), "mistakes": [trap] if trap else [],
              "topic": topic, "difficulty": d, "subtopic": sub,
              "tags": sorted(set(list(tags))), "locator": f"LF{len(Q)+1:02d}"})

q("Walk me up a leveraged capital structure from the most senior instrument to equity.",
  "Super senior revolving credit facility, usually undrawn at close, secured on the same collateral "
  "as the term debt but ranking ahead of it in the enforcement waterfall. It funds working capital "
  "and pays an undrawn commitment fee.\n\n"
  "First lien term debt. Term Loan A, amortising and syndicated to banks, or more commonly in "
  "sponsor deals Term Loan B, bullet, floating over Euribor or SOFR, syndicated to institutional "
  "investors such as CLOs, credit funds and insurers. Alternatively senior secured notes, which are "
  "bonds rather than loans, with call protection instead of free prepayment.\n\n"
  "Second lien. Same collateral, but recovers only after the first lien is repaid in full. Higher "
  "margin, less common in Europe than in the US, and it disappears entirely in stressed markets.\n\n"
  "Senior unsecured notes. No collateral, ranking behind everything secured, and structurally behind "
  "any debt at the operating companies unless guaranteed.\n\n"
  "Subordinated and mezzanine. Contractually subordinated, often part cash and part PIK, sometimes "
  "with warrants or an equity kicker.\n\n"
  "Then shareholder instruments: preference shares or shareholder loans held by the sponsor, and "
  "finally ordinary equity, including management's sweet equity.\n\n"
  "In a mid-market European deal that whole stack is frequently replaced by a single unitranche "
  "facility from one direct lender, plus a super senior revolver, plus the sponsor equity.",
  ["Puts the super senior revolver at the top and explains its ranking in enforcement",
   "Distinguishes TLA from TLB by amortisation and investor base",
   "Places second lien, senior unsecured and subordinated or mezzanine in the correct order",
   "Names shareholder instruments and sweet equity below the debt",
   "Notes that a mid-market European deal often collapses the stack into a unitranche plus a super senior RCF"],
  tags=["levfin-structure", "capital-structure"], d=3,
  trap="Ranking senior unsecured above second lien. Second lien has collateral; unsecured does not.")

q("What is unitranche debt, and why does a mid-market European sponsor deal go to a direct lender rather than syndicate?",
  "Unitranche is a single facility at a single blended margin replacing what would otherwise be "
  "first lien, second lien and sometimes mezzanine. One lender or a small club provides the whole "
  "amount, one set of documents, one set of covenants, one negotiation.\n\n"
  "The sponsor's reasons are speed and certainty. A direct lender can commit on the basis of its own "
  "credit work without a ratings process, without a syndication and without market flex, so the "
  "financing is certain at signing rather than subject to a market that may move. In a competitive "
  "auction, certainty of funds is worth more than a few basis points of margin, because it is what "
  "lets the sponsor bid without a financing condition. Direct lenders will also lend against "
  "adjusted EBITDA and business models banks find difficult, will hold the whole ticket, and offer "
  "flexibility on delayed draw for bolt-ons and on covenant headroom.\n\n"
  "The price is a materially higher margin than a syndicated TLB, and the borrower has a "
  "concentrated relationship: one lender that also holds all the consent rights, which matters if "
  "the business underperforms and an amendment is needed.\n\n"
  "Structurally, unitranche is usually paired with a super senior revolver from a bank, and the "
  "ranking between them is set in an agreement among lenders. That is what makes the structure work: "
  "the bank gets first money out on enforcement in exchange for providing the working capital line "
  "the direct lender does not want to hold.",
  ["Defines unitranche as a single facility at a blended margin replacing multiple tranches",
   "Names certainty and speed as the sponsor's reason, with no ratings process, no syndication and no market flex",
   "Connects it to being able to bid without a financing condition in an auction",
   "Names the cost: higher margin and a concentrated lender relationship",
   "Names the super senior revolver and the agreement among lenders as the standard pairing"],
  tags=["levfin-structure"], d=4,
  trap="Explaining unitranche only as convenience. Certainty of funds in a competitive auction is the commercial driver.")

q("What is an agreement among lenders, and how does a first-out last-out split work?",
  "An AAL sits behind a unitranche and governs the relationship between the lenders sharing it, "
  "without the borrower necessarily seeing the economics. To the borrower there is one facility at "
  "one margin; between the lenders, the risk and the return are carved up.\n\n"
  "In a first-out last-out structure, the facility is divided into a first-out tranche, which is "
  "repaid first on enforcement and receives a lower share of the blended margin, and a last-out "
  "tranche, which absorbs losses first and takes a higher return. A bank typically takes the "
  "first-out, which looks and prices like senior secured debt, while a credit fund takes the "
  "last-out, which behaves like mezzanine.\n\n"
  "The AAL also allocates voting and enforcement rights, which is where it gets contested. Who can "
  "instruct the security agent, who can block an amendment, and what happens if the two tranches "
  "want different things in a restructuring. Those provisions matter enormously in a workout and are "
  "usually invisible until one happens.\n\n"
  "The commercial point is that the AAL is what lets a single instrument serve two lender types with "
  "different risk appetites, which is how unitranche can be priced competitively against a tranched "
  "structure while still paying a credit fund what it needs.",
  ["Defines the AAL as governing lender-to-lender economics behind a single borrower-facing facility",
   "Explains first-out as repaid first at a lower margin and last-out as loss-absorbing at a higher return",
   "Identifies who typically takes each tranche",
   "Names voting and enforcement rights as the contested provisions, decisive in a workout"],
  tags=["levfin-structure"], d=5,
  trap="Assuming the borrower sees the split. It is a lender-side arrangement, which is why enforcement rights are the fight.")

q("What is the difference between a Term Loan A and a Term Loan B?",
  "Investor base first, and everything else follows from it.\n\n"
  "A TLA is pro rata bank debt, syndicated to relationship banks alongside the revolver. Because "
  "banks want their capital back and are capital-constrained, a TLA amortises heavily, often on a "
  "straight-line basis over a five-year tenor, carries maintenance covenants tested quarterly, and "
  "prices at a lower margin.\n\n"
  "A TLB is institutional debt, syndicated to CLOs, credit funds, insurers and mutual funds. Those "
  "investors want duration and yield, not amortisation, so a TLB is typically six to seven years, "
  "with nominal amortisation of around one percent a year and a bullet at maturity, priced wider, "
  "and now usually covenant-lite, meaning the only financial covenant is a springing leverage test "
  "on the revolver.\n\n"
  "So a sponsor funding an LBO prefers a TLB: less cash out of the business each year to service "
  "principal, a longer runway to the exit, and fewer ways to trip into default. The letter is about "
  "who holds it, not about ranking. A TLA and a TLB usually rank identically and share the same "
  "security.",
  ["Identifies the investor base as the primary distinction: pro rata bank debt versus institutional",
   "Contrasts amortisation profiles: heavy straight-line versus nominal with a bullet",
   "Contrasts tenor, pricing and covenant package",
   "Explains why a sponsor prefers a TLB in an LBO",
   "States that the letter denotes the holder, not the ranking"],
  tags=["levfin-structure", "debt-schedule"], d=3,
  trap="Thinking B ranks behind A. They usually rank pari passu on the same security.")

q("What is a covenant-lite loan, and what protection does a lender still have?",
  "Covenant-lite means the loan has no maintenance financial covenants. Traditional bank loans "
  "required the borrower to prove compliance with a leverage or coverage ratio every quarter "
  "regardless of what it was doing, and a breach was an event of default even if nothing had "
  "happened. A cov-lite loan replaces that with incurrence covenants, tested only when the borrower "
  "takes a specified action such as raising debt, paying a dividend, or making an acquisition.\n\n"
  "The lender still has a lot. Incurrence tests genuinely constrain the borrower from leveraging up "
  "or taking cash out. There is a negative pledge and a limitation on liens preventing new secured "
  "debt jumping ahead. The security package and the guarantees are unchanged. Mandatory prepayment "
  "provisions apply on asset disposals and often on excess cash flow. Cross-default and cross-"
  "acceleration provisions still apply. And the revolver usually retains a springing leverage "
  "covenant, tested only when drawings exceed a threshold, typically around forty percent.\n\n"
  "What the lender loses is the early warning and the seat at the table. A maintenance covenant "
  "breaches while there is still enterprise value, forcing the borrower to the negotiating table "
  "when the lender has leverage. Without one, deterioration continues until a payment default or a "
  "maturity, by which point the value has often gone. That is why cov-lite recoveries are a genuine "
  "concern and why the structure spread from bonds into loans as lender competition intensified.",
  ["Defines cov-lite as the absence of maintenance covenants, replaced by incurrence tests",
   "Names what survives: incurrence tests, negative pledge, security and guarantees, mandatory prepayments, cross-default",
   "Names the springing revolver covenant and its typical trigger",
   "Explains what is lost: the early warning and the negotiating leverage while value still exists"],
  tags=["covenants", "levfin-structure"], d=4,
  trap="Saying cov-lite means 'no covenants'. It means no maintenance covenants; the incurrence package is often extensive.")

q("What are EBITDA add-backs in a credit agreement, and why do lenders care so much?",
  "Every ratio in the document, leverage, coverage, and every incurrence basket, is calculated off a "
  "defined Consolidated EBITDA. That definition is negotiated, and it is almost always wider than "
  "reported EBITDA.\n\n"
  "The routine add-backs are uncontroversial: non-cash charges, stock compensation, transaction "
  "costs of the acquisition itself, and genuinely non-recurring restructuring costs. The contested "
  "ones are the run-rate items: cost savings and synergies from actions that have been identified "
  "but not yet taken, added back as though already achieved. Whether those are capped, at ten, "
  "twenty or twenty-five percent of EBITDA or uncapped, whether they need to be realisable within "
  "twelve, eighteen or twenty-four months, and whether they need any third-party verification, are "
  "the negotiation.\n\n"
  "Lenders care because the definition compounds. A twenty percent add-back does not just flatter "
  "the reported leverage ratio; it enlarges every basket that is expressed as a multiple of EBITDA, "
  "so it permits more incremental debt, more restricted payments, more acquisitions and more asset "
  "disposals. A generous definition at signing quietly authorises a materially more leveraged "
  "company two years later, without any further lender consent.\n\n"
  "The related structure to know is the grower basket, expressed as the greater of a fixed amount "
  "and a percentage of EBITDA, so the permission expands as the company grows and, critically, does "
  "not shrink back if EBITDA falls, because the fixed floor holds.",
  ["States that the defined Consolidated EBITDA drives every ratio and every basket",
   "Distinguishes routine add-backs from contested run-rate synergies",
   "Names the negotiated points: cap, realisation period, verification requirement",
   "Explains the compounding effect on baskets and therefore on permitted future leverage",
   "Names grower baskets and the ratchet effect of the fixed floor"],
  tags=["covenants", "levfin-structure"], d=5,
  trap="Treating add-backs as a leverage-ratio issue only. They expand every EBITDA-based basket in the document.")

q("A credit has a springing covenant on the revolver. What does that mean and when does it bite?",
  "The revolving facility carries a single financial covenant, usually a first lien net leverage "
  "test, which is only tested when drawings under the revolver exceed a stated threshold at the "
  "quarter end, typically around thirty-five to forty percent of commitments.\n\n"
  "So in normal conditions, with the revolver undrawn or lightly drawn, there is no financial "
  "covenant anywhere in the structure. The test springs into existence precisely when the company is "
  "drawing on its working capital line, which is to say when it is under cash pressure.\n\n"
  "Two consequences worth naming. It is a reasonable design: the lender gets a covenant exactly when "
  "it needs one, and the borrower is not tripped by a ratio during a period when it is comfortably "
  "funded. But it also creates a perverse incentive at the margin. A borrower approaching the "
  "threshold has a reason not to draw, or to repay just before the quarter end, at exactly the point "
  "when it most needs liquidity. Companies have run themselves short of cash to avoid springing a "
  "test.\n\n"
  "The other point is that the covenant benefits only the revolver lenders, and only they can "
  "accelerate on a breach. Term lenders get nothing directly, though a revolver default will usually "
  "cross-default the term debt, which is how the protection reaches them.",
  ["Explains the mechanism: a single leverage test on the revolver, tested only above a drawing threshold",
   "Gives a rough threshold and notes it is tested at quarter end",
   "Names the design logic: a covenant exactly when the borrower is under cash pressure",
   "Names the perverse incentive to avoid drawing or to repay before the test date",
   "Notes only revolver lenders benefit directly, with term lenders reached through cross-default"],
  tags=["covenants", "debt-schedule"], d=4,
  trap="Missing the quarter-end testing point. Borrowers manage the balance around the test date.")

q("Explain intercreditor: structural versus contractual subordination.",
  "Two entirely different mechanisms, and confusing them is the classic error.\n\n"
  "Contractual subordination is agreed in the documents. A subordinated creditor of the same obligor "
  "agrees that it will not be paid until the senior creditor has been paid, and an intercreditor "
  "agreement sets out payment blockages, standstill periods before it can take enforcement action, "
  "turnover obligations if it receives money it should not have, and who controls the security.\n\n"
  "Structural subordination comes from where the debt sits in the group, not from any agreement. A "
  "creditor lending to the holding company ranks behind every creditor of the operating subsidiaries, "
  "because the subsidiaries' assets and cash flow serve their own creditors first, and the holdco "
  "only receives what is left over as a dividend. No document is needed for that to be true; it "
  "follows from separate legal personality.\n\n"
  "The remedy for structural subordination is upstream guarantees from the operating companies plus "
  "security over their assets, which is why guarantor coverage tests exist: the credit agreement "
  "will require guarantors representing, say, eighty percent of group EBITDA. Where guarantees "
  "cannot be given, because of local financial assistance rules or thin capitalisation limits, the "
  "structural subordination is real and it should show up in the pricing.\n\n"
  "So the first two questions on any leveraged financing are who is the borrower and who guarantees.",
  ["Defines contractual subordination as agreed in an intercreditor agreement, with blockages, standstills and turnover",
   "Defines structural subordination as arising from where debt sits in the group, requiring no agreement",
   "Explains the mechanism: subsidiary creditors are paid before anything reaches the holdco",
   "Names upstream guarantees, security and guarantor coverage tests as the remedy",
   "Notes local law limits on guarantees make the subordination real and priceable"],
  tags=["levfin-structure", "capital-structure", "covenants"], d=4,
  trap="Treating them as two words for the same thing. One is a contract; the other is company law.")

q("How is a leveraged loan priced, and what is the all-in yield?",
  "The margin is quoted over a floating benchmark: Euribor for euro-denominated debt, SOFR for "
  "dollars. So a European TLB might price at Euribor plus 425 basis points. Loans usually include a "
  "zero floor on the benchmark, so a negative Euribor does not reduce the coupon below the margin.\n\n"
  "On top of the margin, the all-in yield includes original issue discount. A loan issued at 98 "
  "instead of par gives the lender an extra two points of return, amortised over the expected life, "
  "which on a seven-year loan assumed to repay in three or four years is worth roughly fifty to "
  "seventy basis points a year. OID is how a deal gets repriced during syndication without reopening "
  "the margin.\n\n"
  "Then there are arrangement, underwriting and ticking fees, which accrue to the arranging banks "
  "rather than to the lenders, and a margin ratchet, which steps the margin down as leverage falls "
  "through defined levels, typically in twenty-five basis point increments.\n\n"
  "When people quote a yield to maturity on a leveraged loan they usually mean yield to a three-year "
  "assumed life, because leveraged loans are prepayable at par after a short soft call period and "
  "very few run to maturity. That assumption is why OID matters so much in the yield calculation: a "
  "shorter assumed life spreads the discount over fewer years.",
  ["Names the floating benchmark plus margin convention, with Euribor for euros and SOFR for dollars",
   "Names the benchmark floor",
   "Explains OID and how it converts into annual yield over an assumed life",
   "Names arrangement and underwriting fees, and the margin ratchet stepping down with leverage",
   "Explains why the yield is quoted to an assumed three-year life rather than to maturity"],
  tags=["levfin-structure", "bond-math"], d=4,
  trap="Quoting a yield to maturity. Leveraged loans are prepaid; the market quotes to an assumed three-year life.")

q("What is call protection, and how does it differ between a loan and a high yield bond?",
  "Call protection restricts the borrower from repaying early, or makes it pay to do so, protecting "
  "the lender's expected return against reinvestment risk.\n\n"
  "A leveraged loan is prepayable at par by the borrower at any time, subject only to a short soft "
  "call: typically 101 for six or twelve months, and only on a repricing, meaning a refinancing at a "
  "lower margin. That is deliberately weak. It means a borrower whose credit improves can reprice "
  "the loan tighter almost immediately, and in a strong market that happens repeatedly.\n\n"
  "A high yield bond has hard call protection. A typical structure is non-call for the first three "
  "or four years of a seven or eight year bond, written NC3, after which it becomes callable on a "
  "declining schedule, often starting at par plus half the coupon and stepping down to par. Before "
  "the first call date, redemption requires a make-whole payment, discounting the remaining cash "
  "flows at the government yield plus a narrow spread, typically fifty basis points, which is "
  "deliberately punitive.\n\n"
  "There are two standard carve-outs in the non-call period: an equity claw, allowing redemption of "
  "typically thirty-five to forty percent of the issue at par plus coupon with the proceeds of an "
  "equity offering, and the change of control put at 101 held by the investor rather than the issuer.\n\n"
  "The practical consequence is that in a falling rate environment the loan tranche gets refinanced "
  "and the bond tranche does not, which is one reason issuers with a view on rates weight the "
  "structure towards loans.",
  ["States a loan is prepayable at par subject to a short soft call, usually 101 on a repricing",
   "States a high yield bond has a hard non-call period, then a declining call schedule",
   "Explains the make-whole and its punitive discount rate before the first call date",
   "Names the equity claw and the change of control put",
   "Draws the consequence: loans get refinanced when rates fall and bonds do not"],
  tags=["liability-management", "levfin-structure"], d=4,
  trap="Assuming call protection is symmetric. Loans have almost none; bonds have a lot.")

q("What is a PIK toggle, and when does it make sense?",
  "A payment in kind toggle lets the issuer choose, each period, whether to pay the coupon in cash "
  "or to capitalise it into the principal. Choosing PIK usually costs a step-up, commonly seventy-"
  "five basis points, to compensate the lender for the deferral and the increased exposure.\n\n"
  "It makes sense where cash is genuinely lumpy rather than genuinely absent: a business with a heavy "
  "capex programme, an integration that consumes cash for eighteen months, or a seasonal working "
  "capital swing. It buys the borrower cash flow flexibility without a covenant amendment or a "
  "restructuring.\n\n"
  "It is also, in practice, a signal. Interest that compounds into principal makes the leverage ratio "
  "worse every period rather than better, so a borrower toggling to PIK is levering up while its "
  "cash flow is deteriorating, which is the reverse of what a leveraged structure is supposed to do. "
  "Lenders and rating agencies read a toggle election as evidence of stress unless the business plan "
  "always contemplated it.\n\n"
  "Related instruments worth distinguishing: straight PIK notes, which always capitalise, and PIK "
  "notes issued at a holding company above the credit group, structurally subordinated and used to "
  "fund a dividend to the sponsor without touching the operating company's covenants. Those are the "
  "aggressive end of the market and they tend to reappear at the top of a cycle.",
  ["Defines the toggle as an election between cash pay and capitalising the coupon, at a step-up",
   "Names legitimate use cases where cash flow is lumpy rather than absent",
   "Explains why an election signals stress: leverage rises as cash flow deteriorates",
   "Distinguishes straight PIK notes and holdco PIK used to fund a sponsor dividend"],
  tags=["levfin-structure"], d=4,
  trap="Presenting a PIK toggle as free flexibility. Electing it compounds leverage at the worst moment.")

q("What is a bridge loan, and why does an underwriting bank take that risk?",
  "Committed financing provided by the arranging banks to ensure an acquisition can close on time, "
  "on the assumption it will be refinanced shortly afterwards by a permanent bond or loan issue. If "
  "the takeout does not happen, the bridge funds and the banks are left holding the paper.\n\n"
  "The bank takes it because on a competitive acquisition, especially a public one, the buyer needs "
  "certain funds at announcement, and the bond market cannot be accessed before the deal is public. "
  "So somebody has to stand behind the number for the period between announcement and the permanent "
  "financing. A bank that cannot offer a bridge cannot compete for the mandate, and the M&A advisory "
  "fee, the financing fees and the eventual bond mandate are all downstream of it.\n\n"
  "The risk is managed with pricing that escalates deliberately: the bridge starts at a rate and "
  "steps up every three months, with a cap, and if it is still outstanding after a year it typically "
  "converts into an exchangeable term loan and then into securities. The whole structure is designed "
  "to be so unattractive to hold that the borrower refinances promptly.\n\n"
  "The failure mode is a market that closes between commitment and takeout, which is when banks end "
  "up with hung bridges on their balance sheets, sold later at a substantial discount. That is a "
  "well-documented way for a leveraged finance business to lose several years of fee income in one "
  "quarter.",
  ["Defines a bridge as committed interim financing pending a permanent takeout",
   "Explains why banks provide it: certain funds at announcement is required to win the mandate",
   "Names the escalating pricing, the cap, and the conversion into a term loan and then securities",
   "Names the failure mode: a market that closes leaves a hung bridge sold at a discount"],
  tags=["levfin-structure"], d=4,
  trap="Describing a bridge as low risk because it is short dated. The risk is that the takeout market closes.")

q("What is the difference between a firm commitment letter and a highly confident letter?",
  "A commitment letter is a binding undertaking by the lender to provide the financing, subject to "
  "conditions set out in the letter. On a leveraged deal it will attach a term sheet and the "
  "conditions will be negotiated down to a defined list, and in a UK public bid the certain funds "
  "regime cuts them to almost nothing.\n\n"
  "A highly confident letter is not a commitment. The bank states that, based on current market "
  "conditions and its knowledge of the credit, it is highly confident it can raise the amount. There "
  "is no obligation to fund and no balance sheet behind it.\n\n"
  "Which one a bidder brings tells the seller a great deal. In a competitive auction, a bid supported "
  "by committed financing is worth materially more than the same number supported by a highly "
  "confident letter, because the seller is choosing between an offer that will close and an offer "
  "that might. On a public bid under the UK Takeover Code the question does not arise at all: cash "
  "confirmation requires certainty, so a highly confident letter cannot support a Rule 2.7 "
  "announcement.\n\n"
  "The other thing to know is market flex. Even a committed letter usually allows the arrangers to "
  "change pricing, and sometimes structure, to get the deal syndicated. Flex does not let them "
  "refuse to fund, but it means the borrower's economics are not fixed at commitment, which is a "
  "distinction sponsors negotiate hard.",
  ["States a commitment letter is binding subject to defined conditions, with balance sheet behind it",
   "States a highly confident letter carries no funding obligation",
   "Explains why the difference is decisive in a competitive auction",
   "Notes a highly confident letter cannot support a UK Rule 2.7 cash confirmation",
   "Names market flex and clarifies it changes economics without allowing a refusal to fund"],
  tags=["levfin-structure", "takeover-code"], d=3,
  trap="Treating a highly confident letter as almost-committed financing. It is a professional opinion, not an obligation.")

q("Why is a lender's base case less optimistic than the equity case, and why does that matter for covenants?",
  "Because the two parties are looking at different halves of the distribution. An equity investor "
  "benefits from the upside, so the case shown to equity investors is built to demonstrate what the "
  "business could achieve. A lender's upside is capped at getting its coupon and principal back, so "
  "what it is underwriting is the downside: whether the business can service the debt if the plan "
  "does not happen.\n\n"
  "There is also a hard mechanical reason for the sponsor to show lenders a lower case. Covenants "
  "are set with headroom against the case presented, typically thirty to thirty-five percent, so a "
  "higher starting EBITDA produces higher absolute covenant thresholds. If the sponsor presents an "
  "aggressive case and the covenant is set thirty percent below it, the business has to hit an "
  "aggressive number less thirty percent, which may be barely below the base plan. Present a "
  "conservative case and the covenant is set thirty percent below a number the business will "
  "comfortably beat.\n\n"
  "So the sponsor has no incentive to stretch its assumptions with lenders beyond what is needed to "
  "get the financing approved. That is the opposite of the incentive in an equity marketing process, "
  "and it is a genuinely counterintuitive point that separates people who have seen a financing from "
  "people who have read about one.\n\n"
  "The practical consequence: the bank case and the equity case in the same model are not an "
  "inconsistency, they are two deliberately different documents.",
  ["Explains the asymmetry: capped upside means the lender underwrites the downside",
   "Explains the mechanical incentive: covenants are set with headroom below the presented case",
   "Gives a rough covenant headroom figure",
   "Concludes the sponsor has no incentive to stretch the lender case, unlike the equity case"],
  tags=["credit-stats", "covenants"], d=4,
  trap="Assuming the sponsor shows lenders the same numbers as equity investors. It deliberately does not, and for a reason that is not about honesty.")

q("What credit statistics would you look at on a leveraged credit, and which one bites first in a downturn?",
  "Four families.\n\n"
  "Leverage: total net debt to EBITDA, and separately first lien net leverage, because the covenant "
  "and the incurrence tests are often struck on the senior number rather than the total.\n\n"
  "Coverage: EBITDA to cash interest, and more usefully EBITDA less capex to cash interest, which "
  "accounts for the fact that a capital-intensive business cannot spend its whole EBITDA on interest. "
  "Fixed charge cover extends that to leases and mandatory amortisation.\n\n"
  "Cash generation: free cash flow after capex, working capital and cash interest, and free cash flow "
  "conversion. This is the one that actually tells you whether the debt gets repaid.\n\n"
  "Liquidity and maturity: cash on hand, undrawn committed facilities, and the maturity wall.\n\n"
  "Which bites first depends on the shape of the downturn, and that is the real answer. In a "
  "demand-led downturn with falling EBITDA and stable rates, the leverage covenant trips first, "
  "because it moves one for one with earnings. In a rate shock with stable earnings, coverage goes "
  "first, because floating rate debt reprices immediately while EBITDA does not. In 2022 and 2023 "
  "that is exactly what happened: leverage multiples looked fine on paper and interest cover "
  "collapsed, which is why coverage became the binding constraint on new deals and why sponsors "
  "started sizing debt off interest cover rather than off a leverage multiple.",
  ["Names leverage on both total and first lien bases",
   "Names coverage, including EBITDA less capex to interest and fixed charge cover",
   "Names free cash flow after capex, working capital and cash interest as the measure that determines repayment",
   "Names liquidity and the maturity wall",
   "Answers the which-bites-first question conditionally: leverage in a demand shock, coverage in a rate shock, with the recent rate cycle as evidence"],
  tags=["credit-stats"], d=4,
  trap="Naming leverage alone. In a floating-rate structure a rate move hits coverage immediately and leverage not at all.")

q("How does a rising rate environment change the amount of debt a sponsor can raise?",
  "Directly and severely, because leveraged debt is floating rate and the binding constraint is "
  "coverage rather than leverage.\n\n"
  "Work it through. Suppose a business has 100 of EBITDA and lenders require interest cover of at "
  "least 2.0 times, so maximum cash interest is 50. If all-in cost is Euribor at 1 percent plus 450 "
  "basis points, that is 5.5 percent, and 50 of interest supports about 900 of debt, so 9.0 times "
  "leverage. Move Euribor to 3.5 percent and the all-in cost is 8 percent, so the same 50 of "
  "interest supports 625 of debt, or 6.25 times. The credit has not changed, the business has not "
  "changed, and the debt quantum has fallen by around thirty percent.\n\n"
  "Three consequences follow. Purchase prices have to fall or sponsors have to write bigger equity "
  "cheques, and since entry multiples are sticky, deal volume falls instead. Structures shift: more "
  "PIK, more preferred equity, more vendor loans and more seller rollover to fill the gap between "
  "what debt will fund and what the seller wants. And returns compress, because the same exit "
  "multiple on a less levered structure produces a lower IRR, which pushes sponsors towards "
  "operational value creation rather than financial engineering.\n\n"
  "The related point is hedging: lenders will typically require a portion of the floating exposure "
  "to be hedged with a cap or a swap, which turns some of that sensitivity into an upfront cost.",
  ["Identifies coverage, not leverage, as the binding constraint on a floating-rate structure",
   "Works a numerical example showing the fall in supportable debt from a benchmark rate move",
   "Names the consequences: lower prices or larger equity cheques, and in practice lower volume",
   "Names structural responses: PIK, preferred, vendor loans, rollover",
   "Notes hedging requirements convert some sensitivity into upfront cost"],
  tags=["credit-stats", "levfin-structure"], d=5,
  trap="Answering that higher rates make debt 'more expensive'. The point is the quantum available falls, which changes what can be bought.")

q("What is a dividend recapitalisation, and how does a lender look at one?",
  "The company raises new debt and pays the proceeds out to its shareholders, normally the sponsor, "
  "as a dividend or a return of capital. No operational change, no acquisition: the debt goes up and "
  "the equity comes out.\n\n"
  "The sponsor's reason is straightforward. It de-risks its position by returning capital before "
  "exit, locks in a return regardless of what the eventual sale achieves, improves the fund's IRR by "
  "pulling cash forward, and lets it hold a good asset longer without a forced exit. On a business "
  "that has deleveraged materially since acquisition, a recap can return the whole original equity "
  "cheque while the sponsor still owns the company.\n\n"
  "A lender looks at it much less warmly. Leverage rises, the equity cushion below the debt shrinks, "
  "and none of the proceeds are invested in the business, so there is no earnings growth to service "
  "the additional debt. Recoveries in a default are lower because there is more debt against the same "
  "assets. Whether it is permitted at all depends on the restricted payments covenant: the "
  "combination of the builder basket, which accumulates from retained cash flow, fixed baskets, and "
  "any ratio-based permission such as being allowed to pay dividends below a stated leverage level.\n\n"
  "Rating agencies treat a recap as evidence about financial policy, which is a rating modifier in "
  "its own right. A company whose owner has shown willingness to lever up for a dividend will be "
  "assumed capable of doing it again.",
  ["Defines it as new debt raised to pay proceeds to shareholders with no operational use",
   "Names the sponsor's motives: de-risking, locking in return, pulling IRR forward, holding longer",
   "Names the lender's objections: higher leverage, thinner equity cushion, no earnings growth, lower recoveries",
   "Names the restricted payments covenant and its builder, fixed and ratio baskets as the gate",
   "Notes rating agencies read it as evidence about financial policy"],
  tags=["levfin-structure", "covenants", "lbo-returns"], d=3,
  trap="Describing it as free money for the sponsor. It is permitted only to the extent the restricted payments covenant allows.")

q("What is the difference between an asset-based revolver and a cash flow revolver?",
  "The difference is what determines how much you can draw.\n\n"
  "An ABL revolver has a borrowing base: availability is a formula applied to eligible current "
  "assets, typically something like eighty to eighty-five percent of eligible receivables and fifty "
  "to sixty-five percent of eligible inventory, with ineligibility rules stripping out old "
  "receivables, concentrations, intercompany balances and obsolete stock. The facility is secured on "
  "those same assets, and the base is recalculated monthly or more often. Covenants are usually "
  "light, often a single springing fixed charge cover test, because the collateral does the work.\n\n"
  "A cash flow revolver sizes availability against the borrower's earnings and cash generation "
  "rather than against specific assets. It has no monitoring of a borrowing base, which is simpler "
  "and cheaper to run, but the lender is exposed to future cash flow rather than to assets it can "
  "seize, so the covenant package is tighter.\n\n"
  "Which one suits depends on the balance sheet. A distributor, a retailer or a manufacturer with "
  "large receivables and inventory can get more availability and cheaper pricing from an ABL. A "
  "services or software business with almost no tangible current assets cannot use one at all, "
  "because the borrowing base would be negligible.\n\n"
  "The important behavioural point is that an ABL shrinks exactly when the business deteriorates: "
  "falling sales reduce receivables, which reduces the borrowing base, which reduces liquidity at "
  "the moment it is most needed. That procyclicality is the main criticism of the structure.",
  ["Explains the borrowing base formula with rough advance rates and eligibility rules",
   "States a cash flow revolver sizes against earnings with a tighter covenant package",
   "Matches each to a balance sheet type: asset-rich versus asset-light",
   "Names the procyclicality of an ABL: availability shrinks as the business deteriorates"],
  tags=["debt-schedule", "levfin-structure"], d=3,
  trap="Assuming an ABL is always cheaper. It is only available at scale to a business with real current assets.")

q("What is the private credit market, and what has its growth changed?",
  "Non-bank lenders, principally credit funds raised by asset managers and by private equity firms' "
  "credit arms, lending directly to companies and holding the loans rather than distributing them. "
  "It has moved from a niche filling gaps beneath the syndicated market to being the primary "
  "financing route for mid-market sponsor deals in Europe, and increasingly for large ones.\n\n"
  "What drove it: bank capital rules made holding leveraged loans expensive, so banks retreated; "
  "institutional investors wanted floating rate yield with low mark-to-market volatility; and "
  "sponsors valued speed and certainty over the last few basis points of margin.\n\n"
  "What it has changed. Financing certainty at signing, because a direct lender commits without "
  "syndication risk or market flex, which changed how sponsors bid in auctions. Documentation, "
  "because a bilateral negotiation with one lender produces more bespoke terms than a syndicated "
  "deal marketed to a hundred accounts. Price discovery, because a private loan has no traded price, "
  "so there is no daily mark and no market signal about deteriorating credit. And workouts, because "
  "a single lender with the whole position can restructure quickly, without the coordination problem "
  "of a syndicate, but also without anyone outside seeing it happen.\n\n"
  "The open question, and the honest thing to say, is that the asset class has grown enormously "
  "without being tested through a full default cycle, and that the absence of marks means "
  "deterioration is visible later than it would be in the public market. That is the substance of "
  "the concern regulators have been raising, and it is a more interesting answer than reciting the "
  "growth numbers.",
  ["Defines private credit as non-bank lenders originating and holding rather than distributing",
   "Names the drivers: bank capital rules, institutional demand for floating rate yield, sponsor demand for certainty",
   "Names the effects on financing certainty, documentation, price discovery and workouts",
   "Raises the untested-cycle and absence-of-marks concern rather than only quoting growth"],
  tags=["levfin-structure"], d=4,
  trap="Reciting market size figures. The interesting answer is what the absence of a traded price does to information.")

q("A sponsor wants to buy a company at 12x EBITDA. Debt markets will fund 5.5x. What are the options?",
  "The equity cheque is 6.5 turns, which on most fund models is too much to make the return work, so "
  "the question is how to close the gap without either raising the debt beyond what lenders will "
  "fund or cutting the price to a level the seller will not accept.\n\n"
  "Raise more debt in a different form. A holdco PIK note above the credit group, structurally "
  "subordinated so it does not count in the senior covenant calculations, adding a turn or two at a "
  "high cost. Or preferred equity from a specialist provider, which is economically debt-like but "
  "sits outside the credit agreement.\n\n"
  "Get the seller to fund part of it. A vendor loan note, deferred consideration, or an earn-out "
  "converting part of the price into a contingent claim. If the seller is a founder staying on, "
  "rollover equity does the same thing and improves alignment.\n\n"
  "Bring in co-investors. Syndicating equity to the fund's LPs or to another sponsor reduces the "
  "cheque from the fund without changing the structure.\n\n"
  "Change what is being bought. A sale and leaseback of the property releases capital that reduces "
  "the equity requirement, at the cost of a permanent rent obligation that lenders will treat as "
  "debt-like anyway. Or carve out a non-core division and sell it post-completion.\n\n"
  "Or accept the arithmetic. If the deal only works with structures that make the capital structure "
  "fragile, the honest advice is that the price is wrong. A sponsor that funds a 12x entry with PIK "
  "and preferred has bought a business whose equity is worthless after a modest miss.",
  ["Frames the problem as closing a 6.5 turn equity gap",
   "Names structurally subordinated debt options: holdco PIK and preferred equity",
   "Names seller-funded options: vendor loan, deferred consideration, earn-out, rollover",
   "Names equity co-investment and asset-level options such as a sale and leaseback",
   "Ends with the honest answer: some of these make the structure fragile and the price may simply be wrong"],
  tags=["levfin-structure", "lbo-returns", "deferred-consideration"], d=5,
  trap="Listing instruments without noting that most of them make the equity more fragile, not less.")

q("What are CLOs, and why do they matter to the leveraged loan market?",
  "A collateralised loan obligation is a fund that buys a diversified portfolio of leveraged loans "
  "and finances itself by issuing tranched notes against them, from AAA at the top down to an "
  "unrated equity tranche. The AAA tranche is the bulk of the capital and is cheap, so the structure "
  "generates a levered return on the equity tranche from the spread between what the loans yield and "
  "what the notes cost.\n\n"
  "They matter because CLOs are the dominant buyer of institutional term loans, holding a large "
  "majority of the market. That has three consequences for how the loan market behaves.\n\n"
  "First, CLO formation drives loan demand. When CLO liabilities are cheap, new vehicles get raised, "
  "and that new capital has to be deployed, which tightens loan spreads regardless of credit "
  "fundamentals. When the AAA market widens, CLO issuance stops and loan demand disappears.\n\n"
  "Second, CLO portfolio constraints shape what can be issued. CLOs have limits on the proportion of "
  "CCC-rated assets, on obligor and industry concentration, and on weighted average rating and "
  "spread tests. A wave of downgrades to CCC forces CLOs to sell, which is a technical seller "
  "unrelated to any view on the individual credit.\n\n"
  "Third, CLOs are term-funded and not mark-to-market in the way a mutual fund is, which makes them "
  "stable holders through volatility. That is why the leveraged loan market did not gap the way "
  "high yield did in several recent selloffs.",
  ["Describes the structure: diversified loan portfolio financed by tranched notes from AAA to equity",
   "States CLOs are the dominant holder of institutional term loans",
   "Explains that CLO formation drives loan demand and therefore spreads, independent of credit",
   "Names portfolio constraints, particularly CCC buckets, and the forced technical selling they cause",
   "Notes CLOs are term-funded and therefore stable holders in volatility"],
  tags=["levfin-structure"], d=4,
  trap="Describing a CLO as a CDO of loans and stopping. The behavioural consequences of CLO constraints are what the question is about.")

q("What is liability management in a stressed leveraged credit, and what is a liability management exercise?",
  "In an ordinary investment grade context, liability management means tender offers, exchange offers "
  "and make-whole calls to manage the maturity profile. In a stressed leveraged credit it means "
  "something more aggressive, and the market calls it an LME.\n\n"
  "The standard moves. A drop-down: the borrower transfers valuable assets, often intellectual "
  "property, into an unrestricted subsidiary outside the credit group, then raises new secured debt "
  "against them, so the new money has first claim on assets the existing lenders thought were their "
  "collateral. An uptier: a majority of lenders agrees to amend the credit agreement to allow a new "
  "super senior facility that they participate in and the minority does not, subordinating the "
  "non-participating lenders. Or a discount exchange, where lenders swap into new paper at a "
  "discount to par in exchange for better ranking or security.\n\n"
  "What makes them possible is document flexibility. Cov-lite documents with generous investment "
  "baskets, permissive unrestricted subsidiary definitions and amendment provisions requiring only a "
  "simple majority created the room. The famous transactions of the past few years were all executed "
  "within the four corners of the agreements.\n\n"
  "The consequence has been a redrafting of the market. Lenders now negotiate for blocker provisions "
  "against drop-downs, for pro rata sharing that requires all-lender consent to change, and for "
  "cooperation agreements among themselves before a stress event. It is worth knowing because it has "
  "turned leveraged credit from a credit analysis exercise into partly a document analysis exercise: "
  "two loans in the same company at the same leverage can have very different expected recoveries "
  "depending on what the covenants permit.",
  ["Distinguishes ordinary liability management from a stressed LME",
   "Describes at least two of drop-down, uptier and discount exchange accurately",
   "Explains that document flexibility, not default, is what enables them",
   "Names lender responses: blockers, pro rata sharing protections, cooperation agreements",
   "Draws the conclusion that recovery now depends on documentation as much as on credit"],
  tags=["liability-management", "covenants", "levfin-structure"], d=5,
  trap="Treating covenants as a formality. LMEs are executed entirely within the documents, which is the point.")

q("What is staple financing, and what is the conflict in it?",
  "A financing package arranged by the sell-side adviser and offered to any bidder in the process, "
  "stapled to the information memorandum. The seller's bank pre-agrees terms and a structure so "
  "bidders know financing is available.\n\n"
  "The seller's reasons are good ones. It sets a floor under what bidders can fund, which is "
  "particularly useful where the credit is unusual or the debt market is uncertain, so bids are not "
  "artificially low because bidders cannot get comfortable with financing. It speeds the process, "
  "because bidders do not each have to run their own financing from scratch. It widens the field to "
  "smaller sponsors who cannot easily arrange financing at speed. And it produces a valuation "
  "signal: the leverage the staple supports tells everyone what the market thinks of the credit.\n\n"
  "The conflict is that the adviser is now on both sides. It is advising the seller on which bid to "
  "accept, and simultaneously offering to lend to the bidders and earning a financing fee if its "
  "staple is taken. That creates an incentive to favour the bidder using the staple, and an "
  "incentive to structure the staple in a way that maximises the financing fee rather than the sale "
  "price. It also means the adviser has seen the buyers' financing models.\n\n"
  "It is managed with information barriers between the M&A team and the financing team, disclosure "
  "to the seller's board, and often a separate adviser to opine on the sale. Bidders are always free "
  "to use their own financing, and the majority do, which limits how much the conflict actually "
  "bites in practice.",
  ["Defines staple financing as a pre-arranged package offered by the sell-side adviser to bidders",
   "Names the seller's benefits: a floor under fundable bids, speed, a wider field, and a valuation signal",
   "States the conflict precisely: the adviser earns a financing fee from the counterparty it is advising against",
   "Names the mitigants: information barriers, disclosure to the board, a separate adviser, and bidders' freedom to use their own financing"],
  tags=["levfin-structure", "deal-process"], d=3,
  trap="Naming the conflict without noting that most bidders use their own financing anyway, which limits it.")

q("How would you size the debt for an LBO from first principles?",
  "Three constraints, and the binding one is whichever is tightest.\n\n"
  "What lenders will lend. Start from the market: what leverage multiple is currently available for "
  "this sector, size and credit quality, split by tranche. Then test it against coverage, because in "
  "a high rate environment the coverage test binds well before the leverage multiple does. Compute "
  "cash interest at the expected all-in rate and check EBITDA less capex to interest against what "
  "lenders require.\n\n"
  "What the business can service and repay. Build the cash flow: EBITDA less cash taxes, less "
  "capex, less working capital, less cash interest, less mandatory amortisation. That has to be "
  "positive with headroom under a downside case, not just under the base case. Then check the "
  "deleveraging path: does the structure get to a leverage level at which the business can be "
  "refinanced or sold in year five.\n\n"
  "What the returns require. Solve backwards. At the entry price, the exit multiple you can defend, "
  "and the EBITDA growth in the plan, what leverage do you need for the equity to hit the fund's "
  "hurdle. If that number is above what lenders will lend or above what the cash flow supports, the "
  "deal does not work at that price.\n\n"
  "Then stress it. Take EBITDA down twenty percent and check whether covenants hold and liquidity "
  "survives. A structure that only works in the base case is not a structure, and the whole point of "
  "the exercise is that the sponsor is buying with someone else's money that has to be repaid "
  "regardless of what happens.",
  ["Names market capacity as the starting point and tests it against coverage rather than leverage alone",
   "Builds the serviceability constraint from cash flow after taxes, capex, working capital, interest and amortisation",
   "Solves backwards from the required equity return to the leverage needed",
   "Identifies that the binding constraint is whichever of the three is tightest",
   "Stresses the structure on a downside case for covenant compliance and liquidity"],
  tags=["levfin-structure", "lbo-returns", "credit-stats"], d=4,
  trap="Sizing off a market leverage multiple alone. In a high rate environment coverage binds first.")

json.dump({
  "title": "Leveraged finance and European private credit",
  "origin": "published",
  "status": "active",
  "note": ("Benchmark rates brought forward to Euribor and SOFR; the Wall Street Prep "
           "source predates the LIBOR transition. Private credit, unitranche, AAL and "
           "the LME material are authored, since no source in the corpus covers them."),
  "items": Q,
}, open(Path(__file__).with_name("04-levfin-private-credit.json"), "w"), indent=1, ensure_ascii=False)
print(f"{len(Q)} items")
