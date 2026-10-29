// src/pages/PublicScreener.tsx
//
// The PUBLIC screener -- a no-login page a prospect can land on.
//
// It reads three public endpoints and nothing else:
//
//   * GET /api/v1/strategy-templates        (curated template catalogue)
//   * GET /api/v1/stats/platform            (platform counters)
//   * GET /api/v1/public/published-results  (backtest results a user
//                                                published for comparison)
//
// It never reads an authenticated endpoint, so there is no logged-in state to
// leak, and it never shows a balance, an equity figure, an XP count, a
// position or an API key.
//
// HONESTY RULES THIS PAGE KEEPS (see frontend/src/__tests__/publicScreener.test.tsx):
//   * Every number comes from the public API. Nothing is hardcoded here.
//   * A metric the backtest never produced reads "Not reported", never 0.00.
//   * A template with no published performance says so.
//   * Every published result states that it is a backtest and over which
//     period it covers.
//   * A result that LOST money is published and ranked like any other.
//     The page must never hide a negative number, and must never
//     describe the feed as a list of winners.

import { useMemo, useState } from "react";
import { format, parseISO } from "date-fns";
import { Helmet } from "react-helmet-async";
import { Link } from "react-router-dom";
import { LineChart, Search, ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Logo } from "@/components/ui/logo";
import {
	usePublicPlatformStats,
	usePublicPublishedResults,
	usePublicStrategyTemplates,
} from "@/lib/api";
import {
	collectArchetypes,
	DEFAULT_SCREENER_FILTERS,
	filterTemplates,
	formatArchetype,
	formatCount,
	formatNumber,
	formatPercent,
	formatSignedCurrency,
	NO_PUBLISHED_BACKTEST,
	NOT_REPORTED,
	PERIOD_LABELS,
	readRiskProfile,
	rewardRiskRatio,
	RESULT_CATEGORY_LABELS,
	type ResultCategory,
	type ScreenerFilters,
} from "@/lib/screener";

const SELECT_CLASS =
	"flex h-9 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring";

const formatDate = (value: string): string => {
	try {
		return format(parseISO(value), "dd MMM yyyy");
	} catch {
		return value;
	}
};

const PublicScreener = () => {
	const [category, setCategory] = useState<ResultCategory>("sharpe_ratio");
	const [filters, setFilters] = useState<ScreenerFilters>(
		DEFAULT_SCREENER_FILTERS,
	);

	const {
		data: templates,
		isLoading: templatesLoading,
		isError: templatesError,
	} = usePublicStrategyTemplates();
	const {
		data: published,
		isLoading: resultsLoading,
		isError: resultsError,
	} = usePublicPublishedResults(category);
	const { data: stats } = usePublicPlatformStats();

	const templateList = useMemo(() => templates ?? [], [templates]);
	const archetypes = useMemo(
		() => collectArchetypes(templateList),
		[templateList],
	);
	const screened = useMemo(
		() => filterTemplates(templateList, filters),
		[templateList, filters],
	);

	const updateFilter = <K extends keyof ScreenerFilters>(
		key: K,
		value: ScreenerFilters[K],
	) => setFilters((current) => ({ ...current, [key]: value }));

	const numberFilter = (value: string): number | null => {
		const parsed = Number.parseFloat(value);
		return Number.isFinite(parsed) ? parsed : null;
	};

	const results = published?.results ?? [];
	// The window the RANKING covers -- never the window a single run covered.
	const rankingWindow = PERIOD_LABELS[published?.period ?? "all_time"] ?? "all time";
	const categoryLabel = RESULT_CATEGORY_LABELS[category];
	const publishedTotal = published?.total ?? results.length;

	return (
		<div className="min-h-screen bg-background">
			<Helmet>
				<title>Public Strategy Screener | DepthSight</title>
				<meta
					name="description"
					content="Browse the strategy templates DepthSight publishes and the backtest results users published for public comparison. No account needed."
				/>
				<meta property="og:title" content="Public Strategy Screener | DepthSight" />
				<meta
					property="og:description"
					content="Real strategy templates and published backtest results, straight from the DepthSight public API."
				/>
				<meta property="og:type" content="website" />
			</Helmet>

			<header className="sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
				<div className="container mx-auto flex h-20 max-w-6xl items-center justify-between">
					<Link to="/" className="flex items-center gap-2">
						<Logo className="h-12" />
					</Link>
					<div className="flex items-center gap-2">
						<Button asChild variant="ghost">
							<Link to="/login">Sign in</Link>
						</Button>
						<Button asChild>
							<Link to="/register">Start free</Link>
						</Button>
					</div>
				</div>
			</header>

			<main className="container mx-auto max-w-6xl px-4 py-10 md:py-16">
				<section className="mx-auto max-w-3xl text-center">
					<h1 className="text-3xl font-bold tracking-tight sm:text-4xl">
						Public strategy screener
					</h1>
					<p className="mt-4 text-lg text-muted-foreground">
						Browse the strategy templates DepthSight publishes, and the backtest
						results users published for public comparison. No account needed --
						every number on this page comes from the DepthSight public API.
					</p>
				</section>

				{/* Platform counters. The backend excludes the seeded demo
				    accounts from these, so they reflect real usage. */}
				{stats && (
					<section className="mx-auto mt-10 grid max-w-3xl grid-cols-1 gap-4 sm:grid-cols-3">
						<Card>
							<CardHeader className="pb-2">
								<CardTitle className="text-sm font-medium text-muted-foreground">
									Strategy templates
								</CardTitle>
							</CardHeader>
							<CardContent className="text-3xl font-bold">
								{formatCount(stats.strategyTemplates)}
							</CardContent>
						</Card>
						<Card>
							<CardHeader className="pb-2">
								<CardTitle className="text-sm font-medium text-muted-foreground">
									Accounts
								</CardTitle>
							</CardHeader>
							<CardContent className="text-3xl font-bold">
								{formatCount(stats.totalUsers)}
							</CardContent>
						</Card>
						<Card>
							<CardHeader className="pb-2">
								<CardTitle className="text-sm font-medium text-muted-foreground">
									Backtests run
								</CardTitle>
							</CardHeader>
							<CardContent className="text-3xl font-bold">
								{formatCount(stats.totalBacktests)}
							</CardContent>
						</Card>
						<p className="sm:col-span-3 text-center text-xs text-muted-foreground">
							These counters exclude the seeded demo accounts, so they reflect
							real accounts rather than demo data.
						</p>
					</section>
				)}

				{/* --- Templates ------------------------------------------------ */}
				<section className="mt-16">
					<div className="mx-auto max-w-3xl text-center">
						<h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
							Curated strategy templates
						</h2>
						<p className="mt-3 text-muted-foreground">
							Each template ships with the risk settings shown below. Screen them
							by archetype and by risk -- every filter runs on the numbers the
							template itself publishes.
						</p>
					</div>

					{/* Filters */}
					<div className="mt-8 grid grid-cols-1 gap-4 rounded-xl border bg-card p-4 sm:grid-cols-2 lg:grid-cols-5">
						<div className="lg:col-span-2">
							<label
								htmlFor="screener-search"
								className="mb-1 block text-xs font-medium text-muted-foreground"
							>
								Search
							</label>
							<div className="relative">
								<Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
								<Input
									id="screener-search"
									className="pl-9"
									placeholder="Name or description"
									value={filters.query}
									onChange={(event) =>
										updateFilter("query", event.target.value)
									}
								/>
							</div>
						</div>
						<div>
							<label
								htmlFor="screener-archetype"
								className="mb-1 block text-xs font-medium text-muted-foreground"
							>
								Archetype
							</label>
							<select
								id="screener-archetype"
								className={`${SELECT_CLASS} w-full`}
								value={filters.archetype}
								onChange={(event) =>
									updateFilter("archetype", event.target.value)
								}
							>
								<option value="">All archetypes</option>
								{archetypes.map((archetype) => (
									<option key={archetype} value={archetype}>
										{formatArchetype(archetype)}
									</option>
								))}
							</select>
						</div>
						<div>
							<label
								htmlFor="screener-stop-loss"
								className="mb-1 block text-xs font-medium text-muted-foreground"
							>
								Max stop loss (%)
							</label>
							<select
								id="screener-stop-loss"
								className={`${SELECT_CLASS} w-full`}
								value={
									filters.maxStopLossPercent === null
										? ""
										: String(filters.maxStopLossPercent)
								}
								onChange={(event) =>
									updateFilter(
										"maxStopLossPercent",
										numberFilter(event.target.value),
									)
								}
							>
								<option value="">Any</option>
								<option value="0.5">0.5</option>
								<option value="1">1</option>
								<option value="2">2</option>
								<option value="3">3</option>
							</select>
						</div>
						<div>
							<label
								htmlFor="screener-sort"
								className="mb-1 block text-xs font-medium text-muted-foreground"
							>
								Sort by
							</label>
							<select
								id="screener-sort"
								className={`${SELECT_CLASS} w-full`}
								value={filters.sort}
								onChange={(event) =>
									updateFilter("sort", event.target.value as ScreenerFilters["sort"])
								}
							>
								<option value="name">Name</option>
								<option value="rewardRisk">Reward-to-risk</option>
								<option value="riskPerTrade">Risk per trade</option>
							</select>
						</div>
					</div>

					{templatesLoading ? (
						<p className="mt-10 text-center text-sm text-muted-foreground">
							Loading the template catalogue...
						</p>
					) : templatesError ? (
						<p className="mt-10 text-center text-sm text-muted-foreground">
							The template catalogue is temporarily unavailable. Please try again
							shortly.
						</p>
					) : screened.length === 0 ? (
						<p className="mt-10 text-center text-sm text-muted-foreground">
							No template matches these filters.
						</p>
					) : (
						<div className="mt-8 grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
							{screened.map((template) => {
								const risk = readRiskProfile(template);
								const ratio = rewardRiskRatio(risk);
								return (
									<Card key={template.slug} className="flex flex-col">
										<CardHeader>
											<div className="flex items-start justify-between gap-2">
												<CardTitle className="text-lg">{template.name}</CardTitle>
												<Badge variant="secondary">
													{formatArchetype(template.archetype)}
												</Badge>
											</div>
											<CardDescription>
												{template.description ?? "No description published."}
											</CardDescription>
										</CardHeader>
										<CardContent className="flex-grow">
											<dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
												<div>
													<dt className="text-xs text-muted-foreground">
														Stop loss
													</dt>
													<dd className="font-medium">
														{formatPercent(risk.stopLossPercent, 2)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Take profit
													</dt>
													<dd className="font-medium">
														{formatPercent(risk.takeProfitPercent, 2)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Risk per trade
													</dt>
													<dd className="font-medium">
														{formatPercent(risk.riskPerTradePercent, 2)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Max open trades
													</dt>
													<dd className="font-medium">
														{formatNumber(risk.maxConcurrentTrades, 0)}
													</dd>
												</div>
											</dl>
											<p className="mt-4 text-xs text-muted-foreground">
												Reward-to-risk {formatNumber(ratio)}:1 is implied by the
												template&apos;s own risk settings. It is a property of
												the configuration, not a measured outcome.
											</p>
											<p className="mt-3 text-sm">
												<span className="text-muted-foreground">
													Published performance:
												</span>{" "}
												<span className="font-medium">
													{NO_PUBLISHED_BACKTEST}
												</span>
											</p>
										</CardContent>
									</Card>
								);
							})}
						</div>
					)}
				</section>

				{/* --- Published results -------------------------------------- */}
				<section className="mt-16">
					<div className="mx-auto max-w-3xl text-center">
						<h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
							Published backtest results
						</h2>
						<p className="mt-3 text-muted-foreground">
							These are backtest results that DepthSight users published for
							public comparison. They are backtests, not live trading, and they
							are not a promise of future results. A result that lost money is
							published here exactly like one that made it: the list is ranked by
							the metric you pick, not by whether the strategy was profitable.
						</p>
					</div>

					<div className="mt-6 flex flex-wrap items-center justify-center gap-2">
						{(Object.keys(RESULT_CATEGORY_LABELS) as ResultCategory[]).map(
							(option) => (
								<Button
									key={option}
									type="button"
									variant={option === category ? "default" : "outline"}
									size="sm"
									onClick={() => setCategory(option)}
								>
									Rank by {RESULT_CATEGORY_LABELS[option]}
								</Button>
							),
						)}
					</div>

					{resultsLoading ? (
						<p className="mt-10 text-center text-sm text-muted-foreground">
							Loading published results...
						</p>
					) : resultsError ? (
						<p className="mt-10 text-center text-sm text-muted-foreground">
							Published results are temporarily unavailable. Please try again
							shortly.
						</p>
					) : results.length === 0 ? (
						<p className="mt-10 text-center text-sm text-muted-foreground">
							No results have been published for public comparison yet. A result
							appears here once its owner shares a finished backtest publicly and
							leaves its public link switched on -- and a result that lost money
							is published just like one that made it.
						</p>
					) : (
						<>
							<p className="mt-4 text-center text-xs text-muted-foreground">
								Ranked by {categoryLabel}. Ranking window: {rankingWindow}. Every
								figure on each card is a backtest measurement; a metric the run
								never produced reads &quot;{NOT_REPORTED}&quot;.
							</p>
							{publishedTotal > results.length && (
								<p className="mt-1 text-center text-xs text-muted-foreground">
									Showing the first {results.length} of {publishedTotal} published
									results.
								</p>
							)}
							<div className="mt-8 grid grid-cols-1 gap-6 md:grid-cols-2">
								{results.map((result) => (
									<Card key={`${result.slug}-${result.category}`}>
										<CardHeader>
											<div className="flex items-start justify-between gap-2">
												<CardTitle className="text-lg">
													{result.strategyName}
												</CardTitle>
												<Badge variant="outline">{result.symbol}</Badge>
											</div>
											<CardDescription>
												Backtest window: {formatDate(result.period.start)} -{" "}
												{formatDate(result.period.end)}
											</CardDescription>
										</CardHeader>
										<CardContent>
											<p className="text-xs text-muted-foreground">
												Position {result.rank} of {publishedTotal} among published
												results
											</p>
											<dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
												<div>
													<dt className="text-xs text-muted-foreground">
														Sharpe ratio
													</dt>
													<dd className="font-medium">
														{formatNumber(result.metrics.sharpeRatio)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Net return
													</dt>
													<dd className="font-medium">
														{formatPercent(result.metrics.netPnlPercent)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Net P&amp;L
													</dt>
													<dd className="font-medium">
														{formatSignedCurrency(result.metrics.netPnl)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Win rate
													</dt>
													<dd className="font-medium">
														{formatPercent(result.metrics.winRate)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Max drawdown
													</dt>
													<dd className="font-medium">
														{formatPercent(result.metrics.maxDrawdown)}
													</dd>
												</div>
												<div>
													<dt className="text-xs text-muted-foreground">
														Trades
													</dt>
													<dd className="font-medium">
														{formatNumber(result.metrics.trades, 0)}
													</dd>
												</div>
											</dl>
											<Button asChild variant="outline" size="sm" className="mt-5">
												<Link to={`/s/${result.slug}`}>Open the full report</Link>
											</Button>
										</CardContent>
									</Card>
								))}
							</div>
						</>
					)}
				</section>

				{/* --- What this page is and is not --------------------------- */}
				<section className="mt-16">
					<Card className="border-border/60">
						<CardHeader>
							<CardTitle className="flex items-center gap-2 text-xl">
								<ShieldCheck className="h-5 w-5 text-primary" />
								What this page shows, and what it never shows
							</CardTitle>
						</CardHeader>
						<CardContent className="grid grid-cols-1 gap-6 md:grid-cols-2">
							<div>
								<h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
									What you are looking at
								</h3>
								<ul className="mt-3 space-y-2 text-sm text-muted-foreground">
									<li>
										The curated strategy templates DepthSight publishes, with the
										risk settings each one ships with.
									</li>
									<li>
										Backtest results users published for public comparison, with
										the period they cover and the metric they are ranked by --
										including the results that lost money.
									</li>
									<li>
										Platform counters, with the seeded demo accounts excluded.
									</li>
								</ul>
							</div>
							<div>
								<h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
									What this page will never show
								</h3>
								<ul className="mt-3 space-y-2 text-sm text-muted-foreground">
									<li>
										Anyone&apos;s balance, equity, XP, positions or API keys. This
										page reads public endpoints only; it never touches an account.
									</li>
									<li>
										A number that was not measured. A metric a backtest never
										produced reads &quot;{NOT_REPORTED}&quot;, and a template
										with no published performance says so.
									</li>
									<li>
										Who published a result. Published results carry the strategy
										and the numbers, never the person behind them.
									</li>
								</ul>
							</div>
						</CardContent>
					</Card>
				</section>
			</main>

			<footer className="border-t bg-muted/40">
				<div className="container mx-auto max-w-6xl px-4 py-14 text-center">
					<LineChart className="mx-auto mb-4 h-10 w-10 text-primary" />
					<h3 className="text-2xl font-bold">
						Want to screen your own strategy ideas?
					</h3>
					<p className="mx-auto mt-2 max-w-xl text-muted-foreground">
						Create a free account and run the same backtest engine on your own
						symbols, timeframes and conditions.
					</p>
					<Button asChild size="lg" className="mt-6">
						<Link to="/register">Create a free account</Link>
					</Button>
				</div>
			</footer>
		</div>
	);
};

export default PublicScreener;
