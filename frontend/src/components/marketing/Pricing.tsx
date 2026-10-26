// src/components/marketing/Pricing.tsx

import { Link } from "react-router-dom";
import { Check, Sparkles, Minus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { usePublicPlans } from "@/lib/api";
import { buildPricingCards, buildSignupNote } from "@/lib/pricing";

/**
 * Marketing pricing section.
 *
 * Everything shown here comes from the backend's plan catalog
 * (`GET /api/v1/public/plans`, built from api/plans_config.yml). There is
 * no hardcoded price or feature list, so the page cannot advertise a tier
 * that differs from the one the backend actually grants.
 */
export const Pricing = () => {
	const { data: catalog, isLoading, isError } = usePublicPlans();
	const cards = buildPricingCards(catalog);
	const signupNote = buildSignupNote(catalog);

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

				{isLoading ? (
					<div className="mx-auto mt-16 max-w-3xl text-center text-sm text-muted-foreground">
						Loading plans…
					</div>
				) : isError || cards.length === 0 ? (
					/*
					 * The pricing table is rendered from the backend's plan
					 * catalog, so it cannot show a price the backend does not
					 * sell. If that catalog cannot be reached we say so rather
					 * than falling back to a hardcoded table that might be
					 * wrong.
					 */
					<div className="mx-auto mt-16 max-w-3xl text-center text-sm text-muted-foreground">
						Our plan list is temporarily unavailable. Please contact us
						and we will send you current pricing.
					</div>
				) : (
					<div className="mx-auto mt-16 grid max-w-3xl grid-cols-1 gap-6 md:grid-cols-2">
						{cards.map((plan) => (
							<div
								key={plan.key}
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
								{plan.lifetimePrice && (
									<p className="mt-1 text-sm text-muted-foreground">
										or {plan.lifetimePrice} once — lifetime access
									</p>
								)}
								<ul className="mt-6 space-y-3">
									{plan.included.map((feature) => (
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
								{plan.excluded.length > 0 && (
									<>
										<p className="mt-6 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
											Not included
										</p>
										<ul className="mt-2 space-y-2">
											{plan.excluded.map((feature) => (
												<li key={feature} className="flex items-start gap-2">
													<Minus className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground/60" />
													<span className="text-sm text-muted-foreground/80">
														{feature}
													</span>
												</li>
											))}
										</ul>
									</>
								)}
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
				)}

				{signupNote && (
					<p className="mx-auto mt-10 max-w-2xl text-center text-sm text-muted-foreground">
						{signupNote}
					</p>
				)}

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
