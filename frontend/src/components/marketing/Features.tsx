// src/components/marketing/Features.tsx

import { UserPlus, FlaskConical, Rocket } from "lucide-react";

const steps = [
	{
		icon: UserPlus,
		step: "Step 1",
		title: "Sign up",
		body: "Email + password. We email you a confirmation link. No credit card. No OKX key yet.",
		minutes: "< 1 min",
	},
	{
		icon: FlaskConical,
		step: "Step 2",
		title: "Start a paper strategy",
		body: "Pick a template (RSI Breakout, Golden Cross, MACD). One click to run it on paper against real OKX candles.",
		minutes: "< 2 min",
	},
	{
		icon: Rocket,
		step: "Step 3",
		title: "Graduate to live",
		body: "Connect your OKX API key with read + trade permissions. Switch your paper strategy to live with one click.",
		minutes: "< 5 min",
	},
];

export const Features = () => {
	return (
		<section className="border-b border-border/40 py-20 sm:py-24">
			<div className="container max-w-screen-xl px-4">
				<div className="mx-auto max-w-2xl text-center">
					<h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
						From signup to first fill in 5 minutes.
					</h2>
					<p className="mt-4 text-lg text-muted-foreground">
						Three steps. Zero spreadsheets. No quant-fund pedigree required.
					</p>
				</div>

				<div className="mx-auto mt-16 grid max-w-5xl grid-cols-1 gap-6 md:grid-cols-3">
					{steps.map(({ icon: Icon, step, title, body, minutes }) => (
						<div
							key={step}
							className="relative rounded-xl border border-border/60 bg-card p-6 transition-colors hover:border-primary/40"
						>
							<div className="mb-4 flex items-center gap-3">
								<div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
									<Icon className="h-5 w-5" />
								</div>
								<span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
									{step}
								</span>
							</div>
							<h3 className="text-xl font-semibold">{title}</h3>
							<p className="mt-2 text-sm text-muted-foreground">{body}</p>
							<div className="mt-4 inline-flex items-center rounded-full bg-secondary px-2.5 py-0.5 text-xs font-medium text-muted-foreground">
								{minutes}
							</div>
						</div>
					))}
				</div>
			</div>
		</section>
	);
};
