// src/components/marketing/Pricing.tsx

import { Link } from "react-router-dom";
import { Check, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";

const plans = [
	{
		name: "Free",
		tagline: "Paper trading + backtests",
		price: "$0",
		period: "forever",
		features: [
			"20 fast backtests / day",
			"Paper trading on every template",
			"Standard blocks (Logic, Indicators, Proximity)",
			"90-day history",
			"Limited trading pairs",
			"10 AI Assistant queries / day",
		],
		cta: "Start free",
		ctaTo: "/register",
		highlighted: false,
	},
	{
		name: "Pro",
		tagline: "Live trading + everything in Free",
		price: "$30",
		period: "per month",
		features: [
			"Unlimited backtests",
			"Live trading on OKX, Binance, Bybit",
			"PRO blocks (Tape, Book, Open Interest)",
			"30 live strategies",
			"Genetic strategy search",
			"Full year of candle history",
			"TradingView webhooks",
		],
		cta: "Start 14-day trial",
		ctaTo: "/register?plan=pro",
		highlighted: true,
		badge: "Most popular",
	},
];

export const Pricing = () => {
	return (
		<section id="pricing" className="border-b border-border/40 py-20 sm:py-24">
			<div className="container max-w-screen-xl px-4">
				<div className="mx-auto max-w-2xl text-center">
					<h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
						Start free. Upgrade when you're ready.
					</h2>
					<p className="mt-4 text-lg text-muted-foreground">
						No credit card to start. Cancel any time.
					</p>
				</div>

				<div className="mx-auto mt-16 grid max-w-3xl grid-cols-1 gap-6 md:grid-cols-2">
					{plans.map((plan) => (
						<div
							key={plan.name}
							className={
								"relative rounded-2xl border p-8 " +
								(plan.highlighted
									? "border-primary/60 bg-card shadow-lg shadow-primary/10"
									: "border-border/60 bg-card")
							}
						>
							{plan.badge && (
								<div className="absolute -top-3 left-1/2 -translate-x-1/2">
									<span className="inline-flex items-center gap-1 rounded-full bg-primary px-3 py-1 text-xs font-semibold text-primary-foreground">
										<Sparkles className="h-3 w-3" />
										{plan.badge}
									</span>
								</div>
							)}
							<h3 className="text-2xl font-bold">{plan.name}</h3>
							<p className="mt-1 text-sm text-muted-foreground">
								{plan.tagline}
							</p>
							<div className="mt-6 flex items-baseline gap-1">
								<span className="text-4xl font-bold tracking-tight">
									{plan.price}
								</span>
								<span className="text-sm text-muted-foreground">
									{plan.period}
								</span>
							</div>
							<ul className="mt-6 space-y-3">
								{plan.features.map((feature) => (
									<li key={feature} className="flex items-start gap-2">
										<Check
											className={
												"mt-0.5 h-4 w-4 shrink-0 " +
												(plan.highlighted
													? "text-primary"
													: "text-muted-foreground")
											}
										/>
										<span className="text-sm text-muted-foreground">
											{feature}
										</span>
									</li>
								))}
							</ul>
							<Button
								asChild
								className="mt-8 w-full"
								variant={plan.highlighted ? "default" : "outline"}
								size="lg"
							>
								<Link to={plan.ctaTo}>{plan.cta}</Link>
							</Button>
						</div>
					))}
				</div>

				<p className="mx-auto mt-10 max-w-2xl text-center text-sm text-muted-foreground">
					Need enterprise features (custom exchange, dedicated infra, SLA)?
					Email{" "}
					<a
						href="mailto:enterprise@depthsight.pro"
						className="font-medium text-primary underline-offset-4 hover:underline"
					>
						enterprise@depthsight.pro
					</a>
					.
				</p>
			</div>
		</section>
	);
};
