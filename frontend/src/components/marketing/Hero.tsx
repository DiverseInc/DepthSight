// src/components/marketing/Hero.tsx

import { Link } from "react-router-dom";
import { ArrowRight, Sparkles, TrendingUp, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";

export const Hero = () => {
	return (
		<section className="relative overflow-hidden border-b border-border/40">
			{/* Background gradient */}
			<div
				aria-hidden="true"
				className="absolute inset-0 -z-10"
				style={{
					background:
						"radial-gradient(ellipse at top, hsl(var(--primary) / 0.15), transparent 60%), radial-gradient(ellipse at bottom right, hsl(var(--profit) / 0.08), transparent 50%)",
				}}
			/>

			<div className="container max-w-screen-xl px-4 py-20 sm:py-28 lg:py-32">
				<div className="mx-auto max-w-3xl text-center">
					<div className="mb-6 inline-flex items-center gap-2 rounded-full border border-border/60 bg-background/50 px-3 py-1 text-xs font-medium text-muted-foreground backdrop-blur">
						<Sparkles className="h-3.5 w-3.5 text-primary" />
						<span>Paper trade first. No credit card. Live in 5 minutes.</span>
					</div>

					<h1 className="text-4xl font-bold tracking-tight sm:text-5xl lg:text-6xl">
						AI-driven strategies
						<br />
						that work while you sleep.
					</h1>

					<p className="mx-auto mt-6 max-w-2xl text-lg text-muted-foreground sm:text-xl">
						DepthSight runs your crypto strategies 24/7 with proper risk
						management, backtesting, and OKX execution. Build it on paper,
						graduate to live when you're ready.
					</p>

					<div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row">
						<Button asChild size="lg" className="w-full sm:w-auto">
							<Link to="/register">
								Start paper trading
								<ArrowRight className="ml-2 h-4 w-4" />
							</Link>
						</Button>
						<Button
							asChild
							size="lg"
							variant="outline"
							className="w-full sm:w-auto"
						>
							<Link to="/login">Sign in</Link>
						</Button>
					</div>

					<div className="mt-12 grid grid-cols-1 gap-4 text-left sm:grid-cols-3">
						<div className="rounded-lg border border-border/60 bg-card/50 p-4 backdrop-blur">
							<TrendingUp className="mb-2 h-5 w-5 text-profit" />
							<h3 className="text-sm font-semibold">20+ strategies</h3>
							<p className="mt-1 text-xs text-muted-foreground">
								RSI, MACD, golden cross, breakout templates ready to deploy.
							</p>
						</div>
						<div className="rounded-lg border border-border/60 bg-card/50 p-4 backdrop-blur">
							<ShieldCheck className="mb-2 h-5 w-5 text-primary" />
							<h3 className="text-sm font-semibold">Risk-first</h3>
							<p className="mt-1 text-xs text-muted-foreground">
								Position sizing, stop-losses, drawdown caps on every strategy.
							</p>
						</div>
						<div className="rounded-lg border border-border/60 bg-card/50 p-4 backdrop-blur">
							<Sparkles className="mb-2 h-5 w-5 text-warning" />
							<h3 className="text-sm font-semibold">AI co-pilot</h3>
							<p className="mt-1 text-xs text-muted-foreground">
								Natural-language strategy ideas → backtest → deploy.
							</p>
						</div>
					</div>
				</div>
			</div>
		</section>
	);
};
