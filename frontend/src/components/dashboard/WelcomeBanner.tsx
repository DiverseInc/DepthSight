// src/components/dashboard/WelcomeBanner.tsx
// Shown at the top of the dashboard when a user has no strategies, positions,
// or backtests yet. Greets them by name and surfaces three quick actions
// to get them trading within 5 minutes.

import {
	ArrowRight,
	FlaskConical,
	KeyRound,
	Sparkles,
	Wallet,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { useStrategies } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";

const QuickAction = ({
	icon: Icon,
	title,
	description,
	href,
	tone,
}: {
	icon: React.ComponentType<{ className?: string }>;
	title: string;
	description: string;
	href: string;
	tone: "primary" | "success" | "warning";
}) => {
	const toneClass =
		tone === "primary"
			? "text-primary bg-primary/10"
			: tone === "success"
				? "text-emerald-500 bg-emerald-500/10"
				: "text-amber-500 bg-amber-500/10";
	return (
		<Link
			to={href}
			className="group flex items-start gap-3 p-4 rounded-xl border border-border/40 bg-card/40 hover:bg-card/80 hover:border-primary/40 transition-all"
		>
			<div
				className={`shrink-0 inline-flex items-center justify-center w-10 h-10 rounded-lg ${toneClass}`}
			>
				<Icon className="w-5 h-5" />
			</div>
			<div className="min-w-0 flex-1">
				<p className="text-sm font-semibold flex items-center gap-1.5">
					{title}
					<ArrowRight className="w-3 h-3 opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition-all" />
				</p>
				<p className="text-xs text-muted-foreground mt-0.5">{description}</p>
			</div>
		</Link>
	);
};

export const WelcomeBanner = () => {
	const { user } = useAuth();
	const { data: strategies } = useStrategies({ mode: "paper" });
	const strategiesCount = strategies?.length ?? 0;

	// Hide the welcome banner once the user has any active strategy —
	// the dashboard widgets will then be more useful than onboarding CTAs.
	if (strategiesCount > 0) return null;

	const firstName =
		user?.username?.split(/[._-]/)[0]?.replace(/^\w/, (c) => c.toUpperCase()) ??
		"there";

	return (
		<Card className="relative overflow-hidden border-primary/20 bg-gradient-to-br from-primary/5 via-card/40 to-card/40">
			{/* Decorative blurred orbs — pure CSS, no images needed */}
			<div
				aria-hidden
				className="pointer-events-none absolute -top-16 -right-16 w-64 h-64 rounded-full bg-primary/10 blur-3xl"
			/>
			<div
				aria-hidden
				className="pointer-events-none absolute -bottom-20 -left-10 w-48 h-48 rounded-full bg-cyan-500/10 blur-3xl"
			/>

			<CardHeader className="relative">
				<div className="flex items-start justify-between gap-4 flex-wrap">
					<div className="space-y-2">
						<Badge
							variant="outline"
							className="bg-primary/10 text-primary border-primary/30 font-medium"
						>
							<Sparkles className="w-3 h-3 mr-1 inline-block" />
							Welcome
						</Badge>
						<CardTitle className="text-2xl md:text-3xl font-bold">
							Hi {firstName}, ready to automate your first trade?
						</CardTitle>
						<CardDescription className="max-w-xl">
							Most traders go from signup to live bot in under 10 minutes.
							Pick a verified template, paper-trade it for 48 hours, then go
							live. Here are the fastest three paths to your first trade.
						</CardDescription>
					</div>
				</div>
			</CardHeader>

			<CardContent className="relative">
				<div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
					<QuickAction
						icon={FlaskConical}
						title="Try a verified template"
						description="6 curated strategies — pick one, customize, go live."
						href="/hub"
						tone="primary"
					/>
					<QuickAction
						icon={Wallet}
						title="Connect an exchange"
						description="Add Binance or Bybit API keys with one click."
						href="/settings"
						tone="success"
					/>
					<QuickAction
						icon={KeyRound}
						title="Set risk kill-switches"
						description="Max drawdown, position size, stop losses."
						href="/settings"
						tone="warning"
					/>
				</div>
				<div className="mt-4 flex items-center gap-3 text-xs text-muted-foreground">
					<span>On the free plan · upgrade anytime in Settings → Billing</span>
					<Button asChild variant="link" size="sm" className="text-xs px-0">
						<Link to="/hub">
							See the full marketplace
							<ArrowRight className="w-3 h-3 ml-1 inline-block" />
						</Link>
					</Button>
				</div>
			</CardContent>
		</Card>
	);
};

export default WelcomeBanner;
