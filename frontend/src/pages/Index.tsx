// src/pages/Index.tsx

import { Home } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ActivePositionsTable } from "@/components/dashboard/ActivePositionsTable";
import { CandleFlowIndicator } from "@/components/dashboard/CandleFlowIndicator";
import { LiveEventFeed } from "@/components/dashboard/LiveEventFeed";
import { PlatformStatsStrip } from "@/components/dashboard/PlatformStatsStrip";
import { PortfolioOverviewWidget } from "@/components/dashboard/PortfolioOverviewWidget";
import { SystemStatus } from "@/components/dashboard/SystemStatus";
import { TopStrategiesTable } from "@/components/dashboard/TopStrategiesTable";
import { TotalPnl } from "@/components/dashboard/TotalPnl";
import { TradingModeBadge } from "@/components/dashboard/TradingModeBadge";
import { WelcomeBanner } from "@/components/dashboard/WelcomeBanner";
import { WizardLauncherCard } from "@/components/dashboard/WizardLauncherCard";
import { XpLevelCard } from "@/components/dashboard/XpLevelCard";
import { PageLayout } from "@/components/layout/PageLayout";

const Index = () => {
	const { t } = useTranslation("index");

	return (
		<PageLayout title={t("pageTitle")} icon={Home}>
			<div className="space-y-6">
				<TradingModeBadge />
				{/* FIX 2026-09-20: per-stream candle-flow indicator. Sits right
					under the trading-mode badge so the user sees "what mode am I
					in" and "is data flowing for that mode" together. */}
				<CandleFlowIndicator />
				<WelcomeBanner />
				<WizardLauncherCard />
				<PlatformStatsStrip />
				<XpLevelCard />
				<PortfolioOverviewWidget />
				<div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
					<div className="lg:col-span-2">
						<TotalPnl />
					</div>
					<SystemStatus />
				</div>
				<ActivePositionsTable />
				<div className="grid grid-cols-1 md:grid-cols-2 gap-6">
					<TopStrategiesTable />
					<LiveEventFeed />
				</div>
			</div>
		</PageLayout>
	);
};
export default Index;
