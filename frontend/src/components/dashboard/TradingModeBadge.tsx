// src/components/dashboard/TradingModeBadge.tsx
//
// FIX 2026-09-20: prominent paper-vs-live mode indicator on the dashboard.
// Before this, the active portfolio mode (PortfolioModeContext) was only
// readable by internal components — users had no clear visual signal
// whether they were about to place a real-money trade or a paper trade.
// This is a safety issue: live-mode mistakes can lose money.
//
// Shows:
//   PAPER MODE  - blue/muted badge, "X paper strategies running"
//   LIVE MODE   - amber/warning badge with a pulsing dot, "X live strategies running"
//                  + a small disclaimer line about real-money risk
//
// Reads the mode from PortfolioModeContext and the running strategy count
// from useStrategies (mode-aware). Pure UI, no new API calls.

import { AlertTriangle, FlaskConical, Wallet } from "lucide-react";
import { Link } from "react-router-dom";
import { usePortfolioMode } from "@/context/PortfolioModeContext";
import { useStrategies } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
} from "@/components/ui/card";

export const TradingModeBadge = () => {
	const { mode } = usePortfolioMode();
	const { data: strategies = [] } = useStrategies({ mode });
	const runningCount = strategies.filter((s) => s.status === "running").length;

	const isLive = mode === "live";

	return (
		<Card
			className={
				isLive
					? "relative overflow-hidden border-amber-500/40 bg-gradient-to-br from-amber-500/10 via-card/40 to-card/40"
					: "relative overflow-hidden border-blue-500/30 bg-gradient-to-br from-blue-500/5 via-card/40 to-card/40"
			}
		>
			{/* Subtle pulsing dot when live — emphasizes the "real money" state */}
			{isLive && (
				<div
					aria-hidden
					className="pointer-events-none absolute -top-12 -right-12 w-48 h-48 rounded-full bg-amber-500/10 blur-3xl animate-pulse"
				/>
			)}
			<CardContent className="relative px-4 py-3">
				<div className="flex items-center justify-between gap-3 flex-wrap">
					<div className="flex items-center gap-3 min-w-0">
						<Badge
							variant="outline"
							className={
								isLive
									? "bg-amber-500/15 text-amber-400 border-amber-500/40 font-semibold tracking-wide"
									: "bg-blue-500/10 text-blue-400 border-blue-500/30 font-semibold tracking-wide"
							}
						>
							{isLive ? (
								<>
									<span className="relative flex h-2 w-2 mr-1.5">
										<span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
										<span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
									</span>
									<Wallet className="w-3 h-3 mr-1 inline-block" />
									LIVE MODE
								</>
							) : (
								<>
									<FlaskConical className="w-3 h-3 mr-1 inline-block" />
									PAPER MODE
								</>
							)}
						</Badge>
						<span className="text-sm text-muted-foreground truncate">
							{runningCount > 0 ? (
								<>
									<span className="font-semibold text-foreground tabular-nums">
										{runningCount}
									</span>{" "}
									{runningCount === 1 ? "strategy" : "strategies"} running
								</>
							) : (
								<>No strategies running in {mode} mode</>
							)}
						</span>
					</div>
					{isLive ? (
						<div className="flex items-center gap-2 text-xs text-amber-300/90">
							<AlertTriangle className="w-3.5 h-3.5" />
							<span>Real money at risk — confirm strategy risk limits before going live.</span>
						</div>
					) : (
						<Button asChild variant="link" size="sm" className="text-xs px-0 h-auto">
							<Link to="/settings">Switch to live →</Link>
						</Button>
					)}
				</div>
			</CardContent>
		</Card>
	);
};

export default TradingModeBadge;
