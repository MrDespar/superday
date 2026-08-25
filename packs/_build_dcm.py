"""Build the DCM syndicate pack from a question-shaped HTML source.

The source is the author's own bond syndicate desk handbook, already laid out
as questions with written answers, so this parses it rather than paying a
model to read it back to us, which would be a paraphrase of something already
exact.

    python packs/_build_dcm.py  ~/Downloads/dcm-syndicate-handbook.html

The handbook itself is not distributed with this repository -- it carries the
fit sections and the working notes that go with them, and only the technical
answers belong in a pack. `packs/01-dcm-syndicate.json` is the built artefact
and is what `ingest-pack` reads; this script is kept for provenance and
because `clean()` carries regressions the test suite still exercises.

Rubrics are authored here rather than lifted, because the source's answers are
scripts to say out loud and a rubric has to be a list of things a grader can
mark present or absent.
"""
import html
import json
import re
import sys
from pathlib import Path

# qid -> (topic, difficulty, [tags], [rubric points], "the trap")
# Sections: B bond basics / C pricing / D syndicate / E Schuldschein
#           F hybrids / G credit & ratings / H macro / I EM / K curveballs
#
# The source's fit sections (A, J) are deliberately not mapped. They are career
# narrative -- the answers are one person's biography, so a rubric over them
# grades whether you are that person. Every unmapped id is reported as skipped.
META = {
 # ---- B: bond basics ----
 "B1": ("dcm",1,["bond-math"],["Names nominal, coupon and frequency, tenor, rank, and optionality","Says the issuer raises principal today and repays at 100 at maturity","Names tradability as what separates a bond from a loan"],"Describing a bond as 'a loan' with no mention of the transferable claim."),
 "B2": ("dcm",1,["bond-math"],["States that the coupon is fixed while the market rate moves","Says the price falls until the yield to maturity matches the market rate","Reaches for the present-value framing unprompted"],"Giving only the intuition and never naming discounting."),
 "B3": ("dcm",2,["bond-math"],["Gives the present value formula: sum of coupons plus principal, discounted","Notes that discounting each cash flow at the matching zero rate is cleaner than one YTM","States the YTM assumes a flat curve and reinvestment at y"],"Saying 'I would have to calculate that' instead of using the annuity shortcut."),
 "B4": ("dcm",1,["bond-math"],["Clean price excludes accrued interest, dirty price is what is actually paid","Explains why: the dirty price sawtooths between coupon dates, ruining comparison","Knows the EUR corporate convention is ACT/ACT (ICMA), annual"],"Giving the definitions without the 'why quote clean'."),
 "B5": ("dcm",2,["bond-math"],["Coupon is fixed on nominal; current yield is coupon over price; YTM is the IRR of all cash flows","Says only the YTM combines coupon, time and price movement","Gives the ordering: for a discount bond coupon < current yield < YTM, reversed for a premium bond"],"Getting the ordering backwards. Derive it: a discount bond has a capital gain to come, so YTM is highest."),
 "B6": ("dcm",2,["bond-math"],["Names the reinvestment-at-YTM assumption","Names the hold-to-maturity assumption","Names yield-to-worst as the measure actually quoted for callable paper"],"Listing the assumptions without naming yield-to-worst."),
 "B7": ("dcm",2,["duration-convexity"],["Macaulay is the PV-weighted average time to receipt, in years","Modified is Macaulay divided by (1+y) and measures price sensitivity","Gives the relation dP/P is approximately minus modified duration times dy, with a worked number"],"Knowing the formula but having no worked number ready."),
 "B8": ("dcm",2,["duration-convexity"],["Longer maturity raises duration","Lower coupon raises duration, because more present value sits at the end","Lower yield raises duration","Uses the zero-coupon case, where Macaulay duration equals maturity, as the proof"],"Reciting three rules with no mechanism behind them."),
 "B9": ("dcm",2,["duration-convexity"],["Defines DV01 as the currency price change for a one basis point yield move","Gives DV01 as approximately modified duration times price times 0.0001","Explains why a desk uses it: hedging is in euros per basis point, not percent"],"Conflating DV01 with duration rather than naming it as the absolute view."),
 "B10": ("dcm",3,["duration-convexity"],["Defines convexity as the curvature of the price-yield relationship, the second derivative","States the asymmetry: prices rise more than duration predicts and fall less","Says positive convexity is paid for through a slightly lower yield","Notes convexity turns negative for callable bonds because the call caps upside"],"Missing the negative convexity on callables, which is what separates an intermediate answer."),
 "B11": ("dcm",2,["bond-math"],["Classifies by coupon (fixed, floater, zero, linker)","Classifies by optionality (callable, puttable, convertible)","Classifies by rank (senior unsecured, secured, subordinated, hybrid)","Notes the Schuldschein is not strictly a bond at all"],"A flat list with no organising axis."),
 "B12": ("dcm",3,["bond-math","spread-mechanics"],["Coupon resets against 3M or 6M Euribor plus a fixed margin","Interest rate duration is short, running only to the next fixing","Spread duration runs over the full life, so a floater does not protect against spread widening","Names the discount margin as the relevant yield measure"],"Saying a floater 'has no duration'. It has no rate duration; it has full spread duration."),
 "B13": ("dcm",2,["spread-mechanics"],["Reads upward-sloping, flat and inverted correctly","Names the inversion as pricing rate cuts and historically a recession signal","Lands on the issuer implication: a steep curve pushes issuance to the front end"],"A macro-class answer that never reaches the issuance decision."),
 "B14": ("dcm",3,["spread-mechanics"],["Defines it as the mid of bid and offer on the fixed leg of a matched-maturity swap","Notes the EUR convention is against 6M Euribor","Explains why it is the reference: tenor-matched, liquid and largely credit-risk-free"],"Confusing the mid-swap reference for bond pricing with the OIS/ESTR discounting curve."),
 "B15": ("dcm",3,["spread-mechanics"],["Swap curve is continuously available at every maturity; Bund benchmarks are few","Bund is not neutral: it carries its own scarcity premium or supply concession","Investors hedge and fund against Euribor, not against Bunds","Notes a pricing update shows both, because the swap spread itself moves"],"Giving only the availability argument and missing that the Bund is not a neutral reference."),
 "B16": ("dcm",3,["new-issue-pricing"],["The spread is fixed first at pricing","The yield follows from the mid-swap rate at the moment of fixing plus the final spread","The coupon is a rounded output, set so the bond prices at or just below par"],"Treating the coupon as the input. It is the output, and it is rounded."),
 "B17": ("dcm",2,["bond-math"],["Names the term premium on an upward-sloping curve","Names higher duration as leverage on a view that rates fall","Names the structural bid: insurers and pension funds duration-matching long liabilities"],"Giving only the yield-pickup answer and missing liability matching."),
 # ---- C: spreads and new issue pricing ----
 "C1": ("dcm",3,["spread-mechanics"],["Reference rate for the tenor, in EUR the mid-swap rate","Credit spread for probability of default and loss given default","Liquidity premium as a separate layer","Option premium where there are call rights, plus the new issue premium on top for a new deal"],"Giving two layers and stopping. Naming liquidity separately is what lets you argue a new benchmark prices inside a stale line."),
 "C2": ("dcm",4,["spread-mechanics"],["G-spread is against the nearest government bond and is crude on maturity mismatch","I-spread is against the interpolated swap rate at the same maturity","Z-spread is the constant add-on to the whole zero curve that reprices the bond","ASW is the spread over Euribor from buying fixed and swapping to floating; OAS is z-spread adjusted for options"],"Treating z-spread and ASW as interchangeable. They converge near par and diverge away from it."),
 "C3": ("dcm",4,["new-issue-pricing","spread-mechanics"],["Builds the issuer's own curve first from outstanding bonds on ASW or z-spread","Interpolates to the target tenor and allows for credit curve steepness","Builds a comparable set second, weighted more heavily the thinner the issuer's own curve","Derives fair value adjusted for liquidity, then adds the new issue premium, then sets IPTs above and tightens"],"Starting with comps. That is what you do when there is no issuer curve, which is the exception."),
 "C4": ("dcm",3,["new-issue-pricing"],["Primary axes: rating or credit quality, sector, tenor","Secondary: size and leverage, currency and format, and how current the levels are","Filters on issuance date, because a low-coupon legacy bond at a deep discount has a different buyer profile"],"Omitting issuance date. Legacy discount paper attracts technical buyers and its spread is not a clean read."),
 "C5": ("dcm",4,["new-issue-pricing"],["Separates the level question from the sensitivity question","Widens the net deliberately and says so, rather than pretending the comp set is clean","Says a high beta means the current point value matters less than where the market cycle is","Connects uncertainty in fair value to a higher new issue premium"],"Not making the last link: uncertainty is paid for in the NIP, which turns pricing into a risk decision rather than arithmetic."),
 "C6": ("dcm",3,["new-issue-pricing"],["Defines it as the concession over fair value on the issuer's own secondary curve","Explains what it compensates: absorbing new paper, duration, trading risk, and the incentive to order at all","Measures it ex post as final reoffer spread minus fair value at the moment of pricing","Can say where it currently sits and in which direction it has moved"],"Quoting a stale level. NIPs move; check before quoting one."),
 "C7": ("dcm",3,["new-issue-pricing"],["Names the liquidity premium as the reason","A new benchmark is index-eligible, quoted and tradeable; an old small line barely trades","Concludes fair value is the observed secondary spread corrected for the liquidity difference"],"Treating the observed secondary spread as fair value without the liquidity correction. This is the most common way a naive fair value goes wrong."),
 "C8": ("dcm",3,["new-issue-pricing"],["IPTs are set deliberately generously relative to the expected landing point","Gives the tightening room as a conditional range, wider when volatility is high","Gives the asymmetry: too generous costs basis points, too tight risks a deal that does not cover"],"Presenting the range as a fixed rule rather than conditional on volatility and how well known the name is."),
 "C9": ("dcm",4,["syndicate-process","new-issue-pricing"],["Watches how much book is lost at each step and, more importantly, who leaves","Distinguishes losing fast money (fine) from losing large real money orders (a warning)","Reads the price limits: a book full of limits at the current level makes the next step expensive","Names next-day secondary performance as the real test"],"Watching only the coverage ratio. Who leaves matters more than how much."),
 "C10": ("dcm",3,["spread-mechanics"],["Macro and risk appetite","Fundamentals: leverage, earnings, rating actions, sector shocks","Technicals: primary supply, fund flows, who has stopped buying","Rate volatility as a driver in its own right, independent of the credit"],"Missing rate volatility as a separate driver. It is why NIPs can rise while secondary spreads stay tight."),
 "C11": ("dcm",3,["duration-convexity","spread-mechanics"],["Rate duration is sensitivity to the risk-free curve; spread duration to the credit spread","Notes they are numerically almost identical for a fixed-coupon bullet","Notes they diverge for a floater: rate duration near zero, spread duration full life","Draws the practical conclusion: buy a floater or asset swap to keep credit and drop rate risk"],"Assuming the two are always the same because they usually are."),
 "C12": ("dcm",4,["spread-mechanics"],["Defines it as swap rate minus matched-maturity Bund yield","Names the separate drivers on each leg: Bund supply and safe-haven demand versus bank credit and hedging flows","Explains why it matters: an issuer pricing over swaps but judging in Bund terms feels the move directly"],"Getting the sign wrong on the supply mechanics. If unsure, give the definition and the drivers and stop."),
 "C13": ("dcm",4,["new-issue-pricing"],["Interpolates between the surrounding points on the issuer's own curve","Allows for credit curves flattening at the long end","Sanity-checks against peers who have actually printed at that tenor","Plays the question back: if there is no market at that tenor, a Schuldschein or private placement may be the right product"],"Guessing harder instead of asking whether the format is wrong."),
 "C14": ("dcm",3,["new-issue-pricing"],["Defines it as the pricing advantage of a green bond over a comparable conventional bond","Explains the source: mandated ESG demand against limited supply","Is appropriately sceptical: the effect is contested, small, and shrinks in tight markets","Names the more robust benefit as investor base diversification and book resilience"],"Either dismissing the greenium entirely or overclaiming it. Both read as unfamiliarity."),
 # ---- D: syndicate ----
 "D1": ("dcm",3,["syndicate-process"],["Origination is the issuer side: relationship, capital structure and funding advice, pitching","Syndicate is the investor and market side: window, fair value, execution, allocation","Explains the reason for the split: syndicate talks to investors daily, so the price recommendation sits there"],"Describing the org chart without the reason for the split. Also: do not caricature origination."),
 "D2": ("dcm",2,["syndicate-process"],["Names origination, syndicate, DCM sales, and the trading desk as separate functions","Names legal/documentation and compliance","Names the issuer side: treasury, its counsel, rating agencies, paying agent, exchange"],"Saying syndicate 'manages secondary trading'. It does not. The trading desk makes the market; syndicate monitors performance."),
 "D3": ("dcm",3,["syndicate-process"],["Preparation: mandate, documentation (EMTN drawdown or standalone), rating, pricing updates","Pre-marketing: investor calls or a short roadshow, sometimes pre-sounding under wall crossing","Execution day: announcement with IPTs in the morning, guidance, books subject, launch, pricing in the afternoon","After pricing: allocation, free to trade, settlement, listing, secondary monitoring"],"Describing a multi-week process. Most EUR benchmark deals are same-day intraday executions."),
 "D4": ("dcm",3,["syndicate-process"],["Global coordinator coordinates structure, process and documentation","Active bookrunner runs the book, makes the price recommendation, holds allocation authority","Passive bookrunner appears on the ticket and in the league table but does not lead price","Notes too many actives slow price discovery"],"Not knowing the active versus passive distinction, which is genuinely load-bearing."),
 "D5": ("dcm",2,["syndicate-process"],["Distribution: different houses reach different investors","Relationship economics: lending banks are rewarded with fee business","Insurance: several opinions on price and window reduce the risk of getting it wrong","Names the cost: coordination effort and shared fees"],"Missing relationship economics, which is how the market actually works."),
 "D6": ("dcm",3,["syndicate-process"],["Pot deal: one common book visible to all bookrunners, allocation decided by issuer with the leads","Retention: each bank gets a fixed allotment to place itself","Names the pot as European IG standard and transparency as its advantage"],"Not knowing which is standard in European IG."),
 "D7": ("dcm",2,["underwriting-economics","syndicate-process"],["Names the issuance fee retained from proceeds and gives it as a range, flagged as a range","Notes the fee is split by role with active bookrunners taking the largest share","Names the indirect economics: secondary trading, hedging, and league table credit driving future mandates"],"Quoting a precise fee figure. These vary and are not public; a confident wrong number is worse than a range."),
 "D8": ("dcm",2,["syndicate-process"],["Names issuer, rating, format and rank, tenor, expected size, bank group with roles, and IPTs","Notes selling restrictions and any green or sustainability-linked format","Says 'benchmark' is often used instead of a size, preserving the issuer's optionality"],"Committing to a size in the announcement when the book has not told you what is achievable."),
 "D9": ("dcm",4,["syndicate-process"],["Opens by saying size is not the most important thing","Coverage ratio, with a sense of what is healthy and what signals it was launched too cheap","Granularity, price sensitivity and where limits sit","Investor quality (real money versus fast money), attrition, and anchor orders"],"Reading only the coverage ratio. A book full of limits at IPT level is not really large."),
 "D10": ("dcm",2,["syndicate-process"],["Defines it as books open only subject to further price revision","Notes it is the last chance for investors to confirm or withdraw before the final spread","Explains the function: it forces the book to be honest before allocation"],"Not being able to define it at all. It comes up naturally in any bookbuild description."),
 "D11": ("dcm",3,["syndicate-process"],["Allocation is on quality, not pro rata","Favours early, unlimited orders, long-term holders, and strategically important accounts","Penalises likely flippers and inflated orders speculating on a scaleback","Notes the issuer formally decides on syndicate's recommendation, informed by the investor database"],"Saying allocation is pro rata. It is a judgement about behaviour."),
 "D12": ("dcm",3,["syndicate-process"],["A few basis points tighter than reoffer","Explains why: investors are rewarded and order again, without the issuer visibly leaving money behind","Names both failure modes: trading wider means pricing was too aggressive, ten tighter means the concession was too generous"],"Treating a big tightening on the break as an unambiguous success. It means the issuer overpaid."),
 "D13": ("dcm",3,["syndicate-process"],["First separates a market move from a deal-specific move by checking the index","Looks for the cause in the book: concentration, too much fast money, tightening too far too late","Feeds the finding into the investor database and the next allocation","Is honest with the issuer"],"Saying 'we stabilise'. There is no formal price stabilisation in corporate bond syndication the way there is in ECM."),
 "D14": ("dcm",3,["syndicate-process"],["Confidential advance contact with a small number of selected investors to test appetite","The investors receive inside information, go on an insider list, and are restricted from trading","Notes it runs through compliance and is used sparingly: hybrids, debut issuers, unusually large size"],"Describing it as a marketing call. The trading restriction is the cost and the whole compliance point."),
 "D15": ("dcm",3,["syndicate-process"],["Issuer not in blackout after results","No major data release or central bank meeting that day","Manageable rate volatility and no supply pile-up in the same sector","Names the seasonality: January and September densest, August dead, market shuts from late November"],"Naming only 'good market conditions' with no specific conditions behind it."),
 "D16": ("dcm",3,["syndicate-process"],["Serves different investor bases in one go: short end to bank books, long end to insurers","Smooths the maturity profile","Explains why it is often cheaper than stretching one maturity for the full size","Names the cost: parallel books and balancing between tranches"],"Missing that this is the same logic as Schuldschein tranching."),
 "D17": ("dcm",3,["liability-management"],["Names tender offer: buying back outstanding bonds for cash, usually alongside a new issue","Names exchange offer: swapping old bonds into new ones","Names make-whole call and defines the discount rate as government yield plus a narrow spread","Names the trigger: a maturity wall or a coupon that no longer fits the rate environment"],"Defining make-whole without the discount rate, which is what makes early calls expensive and why par call periods exist."),
 "D18": ("dcm",2,["syndicate-process"],["EMTN is framework documentation: base prospectus in place, only final terms needed per drawdown","Explains the benefit: speed and cost per transaction, enabling opportunistic and small issuance","Standalone is documented for a single transaction, suiting infrequent issuers"],"Missing the speed point, which is why programmes exist: decision to announcement in hours."),
 "D19": ("dcm",3,["syndicate-process"],["Both are exemptions from US registration","Reg S: distribution outside the US only, lower disclosure and liability","144A: adds distribution to QIBs in the US, with more disclosure and US liability risk","Notes a typical European IG corporate issues Reg S only"],"Describing 144A as 'a US listing'. It is a private resale exemption, not registration."),
 "D20": ("dcm",4,["new-issue-pricing","syndicate-process"],["Market context first: mid-swaps across maturities, the 10y Bund, the credit index, weekly primary volume","The issuer's own curve: every outstanding bond with maturity, coupon, size, ASW and z-spread, and the weekly change","The comparable set with the same fields, plus peers' recent new issues and how they traded","Derivation: interpolated fair value and an indicative new issue range","Identical layout every edition, every number with a source and a timestamp"],"Producing a different layout each week. Changes are supposed to jump out."),
 "D21": ("dcm",4,["syndicate-process"],["Cuts by investor type, geography, order size, and price sensitivity","Analyses attrition across the guidance steps: who dropped out at which level","Compares against similar transactions in the same sector and rating"],"Presenting the book in isolation. It only becomes an analysis in comparison."),
 "D22": ("dcm",3,["syndicate-process","schuldschein"],["Investor-initiated: a specific size, tenor or structure comes to the issuer","Drawn privately under the MTN programme, with no bookbuild or public announcement","Notes there is no league table credit","Frames it as opportunistic funding between public transactions"],"Not knowing that it produces no league table entry, which is why it is invisible from the outside."),
}

META.update({
 # ---- E: Schuldschein and private placements ----
 "E1": ("dcm",3,["schuldschein"],["States it is legally a loan under German law, not a security","Notes transfer is by assignment, not through a clearing system","Notes it behaves economically like a bond: standardised docs, an arranger, institutional distribution"],"Calling it 'a private bond'. The legal characterisation is the foundation of the accounting, the absence of a prospectus and the absence of a secondary market."),
 "E2": ("dcm",3,["schuldschein"],["Legal nature: loan versus security","Rating usually not required versus effectively required","Documentation of 20-30 pages versus a full prospectus","Size from about 20m versus benchmark from 500m, and weeks of process versus intraday","Investor accounting at amortised cost versus mark to market"],"Getting the size and process rows the wrong way round."),
 "E3": ("dcm",4,["schuldschein"],["Names the accounting as the decisive point: amortised cost under HGB, not market value","Concludes spread widening does not flow through the investor's P&L","Draws the two consequences: the investor is structurally a holder, and the market is insensitive to daily volatility","Notes the investor is paid for illiquidity and the absence of a rating"],"Answering 'for the yield'. The accounting treatment explains the entire market."),
 "E4": ("dcm",3,["schuldschein"],["Core is German savings banks, cooperative banks and Landesbanken","Adds insurers, pension vehicles and some asset managers","Notes Asian banks for larger names","Names the common feature: they do their own credit work and want to hold"],"Describing it as a fund investor base. It is largely a bank investor base."),
 "E5": ("dcm",3,["schuldschein"],["Preparation with the issuer: documentation, financials, credit story, structure proposal","Launch to a broad investor list with an indicative spread range per tranche","A marketing and subscription phase over weeks, not an intraday book, with upsizing if demand is strong","Names the core contrast: a bond's binding constraint is the market window, an SSD's is the investors' credit analysis"],"Describing it as a slow bookbuild. The constraint is different in kind, not just in speed."),
 "E6": ("dcm",3,["schuldschein"],["Serves a heterogeneous investor base in one process","Several tenors in parallel, each in fixed and floating against 6M Euribor, with investors self-selecting","For the issuer: smooths the maturity profile and mixes fixed and floating without a separate swap"],"Not connecting it to the floater logic: investors who do not want rate risk take the floating tranche."),
 "E7": ("dcm",3,["schuldschein","covenants"],["Twenty to thirty pages, mostly German law, no prospectus and no prospectus liability, no listing","Light covenant package: pari passu, negative pledge, change of control, cross-default","Occasionally a financial covenant such as a leverage cap, more for weaker credits","Names the trade: time and cost for the issuer, against an investor base that needs information and time"],"Assuming an SSD is more restrictive than a bond. An IG bond's package is lighter still; the SSD is more negotiable, not tighter."),
 "E8": ("dcm",4,["schuldschein"],["Frames it as a decision tree, not a preference","SSD when: no rating or no wish for one, sub-benchmark size, discretion, multiple tenors and formats, first-time issuer","Bond when: rated, needs size, wants a visible liquid curve, intends to be a repeat issuer","Notes it is not either-or: large corporates use both"],"Presenting it as a ranking of products rather than a fit to the issuer's situation."),
 "E9": ("dcm",4,["schuldschein","ratings"],["Uses a shadow rating from the arranger's internal credit analysis","Looks at where comparable SSDs have priced by credit quality, sector, size and tenor","Brackets it: the bank loan market as the floor, a rated bond as the ceiling","Notes the subscription phase itself generates price discovery, slower but more honest than an intraday book"],"Claiming it cannot be priced without a rating."),
 "E10": ("dcm",3,["schuldschein"],["A note issued in the holder's name, not to bearer, outside clearing, transferred by assignment","Names insurers and pension funds as the natural buyers, again for amortised cost and regulatory fit","Notes typically very long tenors, where the public market delivers little"],"Confusing it with a Schuldschein. Both are non-securities, but the NSV is aimed at very long insurer money."),
 "E11": ("dcm",3,["schuldschein"],["A bespoke drawdown under the issuer's EMTN programme","Usually driven by reverse enquiry from an investor with a specific need","No bookbuild, no public announcement, no league table headline","Names the use case: a size and tenor no benchmark bond can serve"],"Confusing it with a Schuldschein. The MTN PP is a security under a programme; the SSD is a loan."),
 "E12": ("dcm",3,["schuldschein"],["Gives annual new volume in the right order of magnitude","Notes the driver is refinancing from unrated issuers, since rated names have a cost advantage in the public market","Notes ESG-linked SSDs are a modest share and the link is rarely fixed contractually"],"Quoting a volume figure without a year attached."),
 "E13": ("dcm",3,["schuldschein"],["For the issuer: no visible curve, limited scalability, a process that takes weeks","For the investor: no liquidity, no external rating, less transparency in monitoring","Makes the structural point: amortised cost suppresses reported volatility, not actual credit risk"],"Listing only issuer drawbacks and missing that prices adjust with a lag in a credit crisis."),
 # ---- F: hybrids ----
 "F1": ("dcm",3,["hybrids"],["Deeply subordinated, generally perpetual, issued by a non-financial company","Names the features: no fixed maturity, ranks just above equity, issuer call after typically 5 or 7 years","Names the coupon reset at call and the right to defer coupons, usually cumulatively","States the point of the structure: partial equity treatment from the rating agencies"],"Confusing a corporate hybrid with a bank AT1. The bank instrument has a write-down or conversion trigger; the corporate one does not."),
 "F2": ("dcm",4,["hybrids","ratings"],["Rating agencies count a correctly structured hybrid as part equity, typically 50% at S&P","Works the arithmetic: half of the issue does not feed the leverage metrics","Names the benefit: defends the rating without diluting shareholders","Names the price: a materially higher coupon for subordination, deferral and extension risk"],"Missing the dilution point, which is the reason a CFO picks a hybrid over an equity raise."),
 "F3": ("dcm",4,["hybrids"],["NC5 or NC7: non-callable for five or seven years, then callable at par","At the call date the coupon resets to mid-swaps for the reset period plus the original margin","Names the step-ups and their sizes and timing","Explains the design logic: big enough to incentivise a call, small enough not to be a legal maturity that would destroy equity credit","Notes the market trades hybrids to first call: yield-to-call and duration-to-call"],"Treating the perpetual maturity as real. The market prices to first call."),
 "F4": ("dcm",4,["hybrids"],["The risk that the issuer does not call at the first call date","Explains the price effect: effective maturity extends abruptly, duration jumps, price falls","Gives the economic condition: an issuer does not call when refinancing costs more than the reset coupon","Names the reputational constraint that makes most IG issuers call anyway"],"Giving only the arithmetic. The repeated-game reputational cost is why marginal calls still happen."),
 "F5": ("dcm",4,["hybrids","new-issue-pricing"],["Starts from the issuer's own senior curve at the tenor to first call","Adds a subordination premium, given as a rough range and flagged as variable","Sanity-checks against other hybrids measured as the premium over their own senior curve, not the absolute spread","Notes the new issue premium on hybrids is systematically higher than on senior"],"Comparing absolute hybrid spreads across issuers. The premium over each issuer's own senior curve is the comparable."),
 "F6": ("dcm",4,["hybrids","ratings"],["Equity credit on the metrics side, typically 50% at S&P, subject to structural criteria","Instrument notching: usually two notches below the issuer rating for an IG name, more for weaker profiles","Notes equity credit is not permanent and can fall away if criteria change","Knows a rating methodology event call exists for exactly that case"],"Conflating the equity credit with the instrument rating. They are two separate effects."),
 "F7": ("dcm",3,["hybrids"],["Specialist hybrid and subordinated debt funds","Insurers reaching for yield","Crossover buyers: IG accounts taking a high-yield-like coupon on credit they already know","Names the framing: IG credit risk with subordinated price behaviour"],"Missing why hybrids sell off harder than their credit quality suggests in a risk-off move."),
 # ---- G: credit and ratings ----
 "G1": ("dcm",1,["ratings"],["Gives the S&P/Fitch and Moody's scales side by side down to at least CCC","Names BBB- / Baa3 as the lowest investment grade notch and BB+ / Ba1 as the highest high yield notch","Knows the modifier convention: +/- for S&P and Fitch, 1/2/3 for Moody's, with 1 strongest"],"Getting Moody's modifier direction wrong, or putting a modifier on AAA."),
 "G2": ("dcm",3,["ratings"],["Explains it as a demand cliff, not a gradual deterioration","Names the mandate constraint: many institutional mandates may only hold investment grade","Names the fallen angel and the forced selling that follows","Notes the spread jump is far larger than one notch justifies, and loops to why hybrids get issued"],"Treating the boundary as one more notch. It is a discontinuity in the buyer base."),
 "G3": ("dcm",2,["ratings"],["Outlook is a medium-term directional view over one to two years","CreditWatch or review is short-term and event-driven, with a decision expected in about ninety days","Notes a watch moves spreads much more than an outlook change, because action is in sight"],"Treating the two as interchangeable degrees of the same signal."),
 "G4": ("dcm",3,["credit-stats","ratings"],["Leverage: net debt to EBITDA, with an explicit sector caveat rather than a hard threshold","Coverage: EBITDA or EBIT to interest, and why it matters more at higher refinancing coupons","Cash flow: free cash flow and FFO to debt, naming FFO/debt as S&P's anchor","Maturity profile and liquidity: what falls due when, committed undrawn facilities, cash on hand"],"Quoting 'below 3.0x for IG' as a hard rule. It invites an immediate counterexample from a regulated utility."),
 "G5": ("dcm",3,["credit-stats"],["EBITDA excludes capex, working capital, interest and tax, which is what debt is serviced from","Gives the case: a capital-intensive company with high EBITDA and persistently negative free cash flow","Notes adjusted EBITDA is definition-dependent and shows how much latitude sits in the add-backs","Concludes free cash flow after capex is the more honest bondholder measure"],"Criticising EBITDA in the abstract with no concrete case where it misleads."),
 "G6": ("dcm",3,["covenants"],["Maintenance: tested on an ongoing basis, typically quarterly; a breach gives a termination right without any action by the company","Incurrence: bites only when the company does something, such as raising debt, paying dividends or selling assets","Gives the three-tier picture: bank loan, then high yield bond, then IG bond with no financial covenants at all","Notes the IG investor relies on the rating, negative pledge, pari passu and a change-of-control put"],"Listing the two definitions without connecting covenant strength to credit quality."),
 "G7": ("dcm",4,["covenants","levfin-structure"],["Negative pledge: an undertaking not to grant security to others without equal treatment of bondholders","Structural subordination: a holdco creditor ranks behind all creditors of the operating subsidiaries","Explains the mechanism: the holdco only gets what flows up as a dividend after the subsidiary level is served","Names the remedy: guarantees from the operating subsidiaries"],"Confusing structural with contractual subordination. Structural comes from where the assets sit, not from the documents."),
 "G8": ("dcm",4,["credit-stats","ratings"],["Business risk: cash flow stability and predictability, cyclicality, market position, pricing power","Financial risk: leverage, coverage, cash generation, maturity profile, liquidity","Names the agency matrix that combines the two into an anchor rating","Modifiers: financial policy, shareholder structure, M&A appetite, commitment to the rating","Ends on the downside question, because the bondholder's upside is capped"],"Running an equity analysis with the sign flipped. Growth is worth less to a creditor than contracted revenue."),
 "G9": ("dcm",3,["credit-stats"],["Gives the debt-funded buyback as the cleanest case: good for EPS, bad for leverage on the same cash flow","Adds debt-funded acquisitions, aggressive dividends, and spin-offs that remove stable cash flow from the credit group","Names the underlying conflict: the shareholder has unlimited upside and benefits from risk, the creditor does not","Connects it to why covenants and change-of-control puts exist"],"Not having a concrete case ready. This question tests whether the valuation instinct has actually been reoriented."),
 # ---- H: macro. Split by whether the answer expires. ----
 "H1": ("markets",2,["market-awareness"],["Gives the deposit facility rate, the main refinancing rate and the marginal lending rate","States the direction and date of the most recent decision","Notes whether the Council is committing to a path or staying data-dependent"],"Quoting a rate without knowing when it was last changed."),
 "H2": ("markets",3,["market-awareness"],["Identifies the driver as a supply-side energy shock rather than demand","Names second-round effects into wages and services as the actual policy question","Explains why a supply shock is the worst case: inflation up and growth down at once"],"Explaining a hike with demand-side reasoning when the driver was energy."),
 "H3": ("markets",2,["market-awareness"],["Gives the headline euro area rate and the direction of travel","Breaks it into energy, services, non-energy industrial goods and food","Identifies services inflation as the number that matters for second-round effects"],"Quoting a 'core' figure without saying which definition, since core definitions vary."),
 "H4": ("markets",2,["market-awareness"],["Gives the 10y Bund level and how it has moved over the month and year","Names the two drivers: inflation and rate expectations, and supply","Explains the supply side: record German issuance while the ECB no longer reinvests"],"Quoting a level from memory that is a week old. This moves daily."),
 "H5": ("markets",2,["market-awareness"],["Gives the target range and the date and outcome of the most recent meeting","Names the current chair","Explains why it matters for EUR: the rate differential and cross-currency basis drive EUR versus USD issuance"],"Treating the Fed as background. The divergence is what drives the issuance arbitrage."),
 "H6": ("markets",4,["market-awareness","spread-mechanics"],["Channel one, energy and inflation: higher oil lifts the whole level of yields and works against bonds","Channel two, risk aversion: flight to quality bids govvies and widens credit spreads","Channel three, volatility and windows: thinner books, higher new issue premiums, narrower windows","States explicitly that the inflation channel dominates the flight-to-quality channel","Notes the stress shows up in the primary market, not in secondary spreads"],"The naive answer: 'conflict means flight to quality means bonds rally'. The rate channel dominates."),
 "H7": ("markets",3,["market-awareness"],["Gives the oil level and the year-on-year move","Runs the chain: oil up, inflation up, central bank tighter, reference rate up, bond price down regardless of credit","Differentiates the credit channel by sector: energy-intensive industry hurt, producers helped, regulated utilities pass through","Names the third effect on issuance activity itself"],"Saying 'spreads widen' undifferentiated. The sector split is what makes it an analyst answer."),
 "H8": ("markets",4,["market-awareness","spread-mechanics"],["Names the runoff of APP and PEPP with no reinvestment of redemptions","Identifies the key concept: a large price-insensitive buyer has been removed","Consequence one: the private sector absorbs a higher net volume","Consequence two, and the more important one: the market is more sensitive to supply and flows, so a supply pile-up feeds through to premiums"],"Framing QT purely as volume. It was that the ECB bought regardless of price."),
 "H9": ("markets",4,["market-awareness","new-issue-pricing"],["Describes the market as two-tier","Secondary: spreads historically tight, credit expensive against its own history","Primary: new issue premiums up, books thinner, investors more price sensitive, driven by rate volatility not credit","Draws the conclusion: the investor is paid by the rate, not by the credit risk premium"],"Reading the tight index level as an easy market. Execution is where the stress is."),
 "H10": ("markets",4,["market-awareness"],["Names an identifiable macro escalation risk with the transmission spelled out","Names credit valuation itself: tight spreads price in little tolerance for error","Frames it as an asymmetry rather than a forecast","Distinguishes what is stressed (execution, concessions) from what is not (defaults, downgrades)"],"Forecasting. 'That is not a prediction, it is an asymmetry' is the right register for a candidate."),
 "H11": ("markets",2,["syndicate-process"],["August is seasonally dead: investors unstaffed, nobody issues into a thin book","September reopens and the market runs densely until late November","From December books close and balance sheet dates approach","Concludes anyone funding in Q4 has to do it in October and November"],"Not knowing the calendar of the job you are applying to."),
 # ---- I: emerging markets ----
 "I1": ("dcm",3,["syndicate-process"],["Names CEE corporates, plus Turkish and occasionally Middle Eastern issuers","Currency matching: revenues, cost base or acquisition targets in euros","A European investor base that knows the region","Cross-currency basis can make EUR cheaper all-in than USD"],"Assuming EM issuance in EUR is only about diversification. Currency matching usually comes first."),
 "I2": ("dcm",4,["syndicate-process","ratings"],["Format: usually Reg S only, which limits the buyer base to Europe and Asia","Investor base: dedicated EM credit funds plus crossover buyers reaching for yield","Price: wider spreads and systematically higher new issue premiums","Book: more price sensitive, less granular, dependent on a few large accounts","Names the sovereign ceiling as the structural anchor on the rating and therefore the pricing"],"Missing the sovereign ceiling, which anchors the pricing conversation from the outset."),
 "I3": ("dcm",2,["syndicate-process"],["Argues from structure: banks on the ground and house-bank relationships in the region","Notes access comes out of an existing lending relationship rather than a cold pitch","Explains the consequence: the desk sees EM flow a purely domestic house would not"],"Arguing from adjectives rather than from structure."),
 # ---- K: curveballs and calculations ----
 "K1": ("dcm",2,["bond-math"],["Explains lending to a company for interest and repayment, in plain language","Includes the tradability point, which is what makes it a bond","Resists adding technical detail once started"],"Adding jargon halfway through. The discipline of staying simple is the test."),
 "K2": ("dcm",2,["duration-convexity"],["Gives the arithmetic first: roughly a 4.5% price loss, 6 times 0.75","Refines: positive convexity means the actual loss is slightly smaller than the linear approximation","Refines: a simultaneous spread move adds on top via spread duration"],"Leading with the caveats. It sounds evasive; lead with the number, then refine."),
 "K3": ("dcm",4,["new-issue-pricing","spread-mechanics"],["Reads the curve: 30bp over five years is a normal upward-sloping credit curve","Interpolates to the target tenor and states the number","Adjusts downwards for credit curves flattening as tenor extends","Questions the data: checks whether both levels are actually tradeable, since an illiquid long line carries a liquidity premium not a credit premium"],"Interpolating and stopping. Questioning the data quality is what a desk person does automatically."),
 "K4": ("dcm",4,["syndicate-process","new-issue-pricing"],["Reads a 6x book at IPTs as a signal the deal was launched too generously","Tightens substantially, watching how much book is lost and whether large real money stays","Raises the upsize option and asks what the issuer is optimising for, money or price","Declines to go to the last basis point, because a worse break is expensively bought"],"Only tightening. A heavily covered book is an opportunity on two axes, size as well as spread."),
 "K5": ("dcm",4,["syndicate-process"],["Splits the question: does the issuer have to print today, or want to","If it must: reduces execution risk with conservative IPTs, a more generous premium, possibly shorter tenor or smaller size, and an anchor order","If opportunistic: advises waiting, but only on condition you can name what you are waiting for","Notes 'for better conditions' is not a plan; 'until after Thursday's ECB meeting' is"],"Advising to wait with no named event to wait for."),
 "K6": ("dcm",3,["syndicate-process"],["Names a specific issuer, rating, sector, tenor and size","Gives IPT, final spread and therefore the tightening","Gives the book size or coverage","Adds one concrete observation: the premium relative to the environment, the investor distribution, or how it traded"],"A deal you cannot give numbers for. Without IPT and final spread it is not a deal you followed."),
 "K7": ("dcm",4,["market-awareness","credit-stats"],["Splits the answer into a rates decision and a credit decision","Takes a position on duration with a reason","Takes a position on credit quality with a reason, connected to where spreads are","Closes with an honest caveat about the absence of a track record"],"Naming a security. The structure is the answer, not the bond."),
})


def script(a: str) -> str:
    """Take the quote marks off an answer, keeping what they separated.

    The handbook writes each answer as a script to say out loud and quotes it.
    In a pack the whole field is the answer -- `drill` and `show` reveal it
    under a heading that already says so -- and the quotes are noise the
    SOURCES pane then doubles, because it quotes what it displays.

    Some answers carry a second register after the closing quote: a worked
    example, or an aside to the reader rather than to the interviewer ("worked
    example if he wants one"). The quote marks were the only thing dividing
    the two, and `ui.body` folds a bare line into the paragraph above it, so
    dropping them without doing anything else would run a coaching note onto
    the end of the spoken answer. The remainder becomes its own paragraph,
    which is a break the renderer honours.

    An answer that does not open with a quote is left exactly as it is: the
    sectioned ones (C6) and the template ones (K6) quote a script *inside*
    prose, where the marks are doing real work.
    """
    s = a.strip()
    if not s.startswith('"'):
        return a
    close = s.find('"', 1)
    if close == -1:
        return a
    spoken, rest = s[1:close].strip(), s[close + 1:].strip()
    return f"{spoken}\n\n{rest}" if rest else spoken


def clean(x: str) -> str:
    x = re.sub(r"(?s)<(script|style).*?</\1>", " ", x)
    x = re.sub(r"(?i)<br\s*/?>", "\n", x)
    # Table cells before the blanket tag strip, or they concatenate: the
    # handbook's comparison tables landed as "Legal natureloan (German
    # law)security", which is content that survived ingestion and is still
    # unusable. A row becomes a bullet rather than a bare line because
    # ui.body reflows a plain line into the paragraph above it -- the same
    # reason <li> is already turned into "- " below.
    x = re.sub(r"(?i)</t[dh]>\s*", " | ", x)
    x = re.sub(r"(?i)<tr[^>]*>", "- ", x)
    # The break after a table has to outlive the blank-line collapse below,
    # or the sentence following the table is read as a continuation of the
    # last row and rendered inside it. A sentinel is the cheap way to keep
    # one paragraph break without loosening the collapse for the whole file.
    x = re.sub(r"(?i)</table>", "\x00", x)
    x = re.sub(r"(?i)</(p|li|div|h\d|tr|thead|tbody)>", "\n", x)
    x = re.sub(r"(?i)<li[^>]*>", "- ", x)
    x = re.sub(r"(?s)<[^>]+>", "", x)
    x = html.unescape(x)
    x = x.replace("—", " - ").replace("–", "-")
    x = re.sub(r"[ \t]+", " ", x)
    # Empty corner and spacer cells are real in these tables (a rowspan label
    # column leaves one on most rows), so the separators they leave behind
    # come in runs and have to be stripped as runs, not one at a time.
    x = re.sub(r"(?m)(\s*\|)+\s*$", "", x)
    x = re.sub(r"(?m)^-(\s*\|)+\s*", "- ", x)
    x = re.sub(r"(?m)^\s*-\s*$", "", x)
    x = re.sub(r"\n\s*\n+", "\n", x)
    x = re.sub(r"\s*\x00\s*", "\n\n", x)
    return x.strip()


def blocks(src: str):
    for b in re.findall(r'(?s)<details class="qa".*?</details>', src):
        g = lambda p: (re.search(p, b, re.S).group(1) if re.search(p, b, re.S) else "")
        yield {
            "id": clean(g(r'<span class="qid">(.*?)</span>')),
            "q_de": clean(g(r'<span class="qtext">(.*?)</span>')),
            "q_en": clean(g(r'<span class="qen">(.*?)</span>')),
            "a_de": clean(g(r'(?s)<div class="ans de"[^>]*>(.*?)</div>\s*(?=<div class="ans en")')),
            "a_en": clean(g(r'(?s)<div class="ans en"[^>]*>(.*?)</div>\s*(?=<div class="note-block"|</div>\s*</details>|$)')),
        }


def main(path: Path, out: Path) -> None:
    src = path.read_text(encoding="utf-8", errors="replace")
    items, skipped = [], []
    for b in blocks(src):
        meta = META.get(b["id"])
        if not meta:
            skipped.append(b["id"])
            continue
        topic, diff, tags, rubric, trap = meta
        q = (b["q_en"] or b["q_de"]).strip()
        # "Walk me through pricing a new issue." is an instruction, not a
        # question. Blanket-appending "?" turned a third of the handbook into
        # sentences no interviewer would say out loud.
        if not q.endswith(("?", ".", "!")):
            imperative = q.split()[0].lower() in {
                "walk", "tell", "explain", "give", "name", "describe", "compare"}
            q += "." if imperative else "?"

        # English only. The source carries a German script beside each answer,
        # but it is a translation of the English rather than extra content, and
        # a bank that ships every answer twice is one most readers cannot read
        # half of. `q_de` / `a_de` are still parsed, so restoring the bilingual
        # form is a one-line change rather than a re-parse.
        items.append({
            "q": q, "a": script(b["a_en"]),
            "rubric": rubric,
            "mistakes": [trap],
            "topic": topic, "difficulty": diff,
            "subtopic": None,
            "tags": sorted(set(tags + ["dcm-syndicate"])),
            "locator": b["id"],
            # H1 and H4 are bound to live data further down the pipeline; the
            # rest of section H is analysis whose shape does not expire.
            "kind": "market_awareness" if b["id"] in {"H1", "H3", "H4", "H5", "H7", "H9"} else None,
        })
    for it in items:
        if it["kind"] is None:
            del it["kind"]
    pack = {
        "title": "DCM syndicate desk question set",
        "origin": "published",
        # Landed active: these are not a model's extraction. They are written
        # answers from a prepared, fact-checked document, parsed rather than
        # paraphrased, against rubrics authored here.
        "status": "active",
        "note": ("Bond syndicate, pricing, Schuldschein, hybrids, ratings and "
                 "macro. Authored: the answers are parsed from the author's own "
                 "desk handbook, where each is written as a script to say out "
                 "loud, and the rubrics are written for this repo. "
                 "Six items are kind=market_awareness: their answer is a dated "
                 "snapshot bound to live data at drill time, not a stored fact."),
        "items": items,
    }
    out.write_text(json.dumps(pack, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(items)} items -> {out}")
    print(f"skipped (unmapped, including the fit sections): {skipped}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser(),
         Path(__file__).with_name("01-dcm-syndicate.json"))
