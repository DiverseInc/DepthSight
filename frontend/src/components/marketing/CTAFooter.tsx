// src/components/marketing/CTAFooter.tsx

import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";

export const CTAFooter = () => {
	return (
		<section className="py-20 sm:py-24">
			<div className="container max-w-screen-xl px-4">
				<div className="mx-auto max-w-3xl rounded-2xl border border-border/60 bg-gradient-to-br from-primary/10 via-card to-card p-12 text-center">
					<h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
						Stop guessing. Start automating.
					</h2>
					<p className="mx-auto mt-4 max-w-xl text-lg text-muted-foreground">
						Join traders running 24/7 strategies on OKX, Binance, and Bybit —
						with proper risk management built in.
					</p>
					<div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
						<Button asChild size="lg" className="w-full sm:w-auto">
							<Link to="/register">
								Start paper trading
								<ArrowRight className="ml-2 h-4 w-4" />
							</Link>
						</Button>
						<Button
							asChild
							size="lg"
							variant="ghost"
							className="w-full sm:w-auto"
						>
							<Link to="/login">I already have an account</Link>
						</Button>
					</div>
				</div>
			</div>
		</section>
	);
};
