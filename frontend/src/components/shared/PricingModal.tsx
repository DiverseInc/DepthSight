// src/components/shared/PricingModal.tsx
//
// The plan comparison a customer sees on the Account page.
//
// There is NO self-serve purchase here, and there is no checkout either.
// The payment provider is opt-in: api/routes/payments.py raises "Payment
// provider is not configured." unless BITCART_API_URL, BITCART_API_KEY and
// BITCART_STORE_ID are all set, and the default deployment does not set
// them (deploy.sh:135 sets START_BITCART="n"). A crypto checkout that is
// wired up would therefore always fail at the last step.
//
// The honest path is the one the rest of the product describes: a paid tier
// is set up by us, after the customer contacts us. So this modal shows what
// each plan includes and sends the customer to a support ticket (category
// "Billing / Plans"), which is where a request for a paid tier actually
// gets answered.

import {
	CheckCircle2,
	Clock,
	Info,
	Terminal,
} from "lucide-react";
import type React from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
	CardDescription,
	CardFooter,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/use-toast";
import { type Plan, usePlans } from "@/lib/api";
import { cn } from "@/lib/utils";

interface PricingModalProps {
	isOpen: boolean;
	onClose: () => void;
	currentPlan: string;
}

// --- Plan Card ---
const PlanCard: React.FC<{
	plan: Plan;
	isCurrent: boolean;
	onRequest: (plan: Plan) => void;
}> = ({ plan, isCurrent, onRequest }) => {
	const { t } = useTranslation("common");
	return (
		<Card
			className={cn(
				"flex flex-col",
				isCurrent && "border-primary ring-2 ring-primary",
			)}
		>
			<CardHeader>
				<CardTitle className="text-xl">{plan.name}</CardTitle>
				<CardDescription className="text-3xl font-bold">
					${plan.price_usd}
					<span className="text-sm font-normal text-muted-foreground">
						{" "}
						/mo
					</span>
				</CardDescription>
			</CardHeader>
			<CardContent className="flex-1 space-y-3">
				<p className="text-sm text-muted-foreground h-16">{plan.description}</p>
				<ul className="space-y-2">
					{/*
					 * Prefer the entitlements derived from the real
					 * quotas/permissions/limits. `plan.features` is hand-written
					 * copy and has drifted from the backend before (the $100
					 * tier used to promise "Unlimited AI Assistant" while its
					 * quota is 50 queries a day).
					 */}
					{(plan.entitlements?.length
						? plan.entitlements
								.filter((entitlement) => entitlement.included)
								.map((entitlement) => entitlement.label)
						: plan.features
					).map((feature, index) => {
						const lowerFeature = feature.toLowerCase();
						const isLimitation =
							lowerFeature.includes("limited") ||
							lowerFeature.includes("(30 days)");
						const Icon = isLimitation ? Clock : CheckCircle2;
						const iconColor = isLimitation
							? "text-yellow-500"
							: "text-green-500";

						return (
							<li key={index} className="flex items-start">
								<Icon
									className={cn("h-5 w-5 mr-2 mt-0.5 flex-shrink-0", iconColor)}
								/>
								<span className="text-sm">{feature}</span>
							</li>
						);
					})}
				</ul>
			</CardContent>
			<CardFooter>
				<Button
					className="w-full"
					onClick={() => onRequest(plan)}
					disabled={isCurrent || plan.key === "free"}
				>
					{isCurrent
						? t("pricingModal.currentPlan")
						: plan.key === "free"
							? t("pricingModal.downgradeNotAvailable")
							: t("pricingModal.selectPlan")}
				</Button>
			</CardFooter>
		</Card>
	);
};

export const PricingModal: React.FC<PricingModalProps> = ({
	isOpen,
	onClose,
	currentPlan,
}) => {
	const { t } = useTranslation("common");
	const { toast } = useToast();
	const navigate = useNavigate();
	const { data: plansData, isLoading, isError, error } = usePlans({
		refetchInterval: isOpen ? 10000 : false,
	});

	// A paid tier is set up by us, so the button does not buy anything: it
	// takes the customer to a support ticket and tells them which plan to
	// ask for.
	const handleRequest = (plan: Plan) => {
		onClose();
		toast({
			title: t("pricingModal.requestTitle", {
				defaultValue: `Request the ${plan.name} plan`,
			}),
			description: t("pricingModal.requestDescription", {
				defaultValue:
					"Open a support ticket under Billing / Plans and tell us which plan you want. We set it up for you.",
			}),
		});
		navigate("/support");
	};

	return (
		<Dialog open={isOpen} onOpenChange={onClose}>
			<DialogContent className="max-w-4xl">
				<DialogHeader>
					<DialogTitle className="text-3xl font-bold">
						{t("pricingModal.title")}
					</DialogTitle>
					<DialogDescription>
						{t("pricingModal.description")}
					</DialogDescription>
				</DialogHeader>

				<div className="py-6">
					<Alert className="mb-6 bg-muted/50 border-none">
						<Info className="h-4 w-4 text-primary" />
						<AlertDescription className="text-sm font-medium">
							{t("pricingModal.agreementNotice.blocksNote")}
						</AlertDescription>
					</Alert>

					{isLoading && (
						<div className="grid grid-cols-1 md:grid-cols-3 gap-6">
							{[...Array(3)].map((_, i) => (
								<Card key={i}>
									<CardHeader>
										<Skeleton className="h-6 w-1/2" />
									</CardHeader>
									<CardContent className="space-y-3">
										<Skeleton className="h-5 w-3/4" />
										<Skeleton className="h-16 w-full" />
										<Skeleton className="h-10 w-full" />
									</CardContent>
								</Card>
							))}
						</div>
					)}
					{isError && (
						<Alert variant="destructive">
							<Terminal className="h-4 w-4" />
							<AlertTitle>{t("errorTitle")}</AlertTitle>
							<AlertDescription>{error.message}</AlertDescription>
						</Alert>
					)}
					{plansData && (
						<div className="space-y-5">
							<div className="grid grid-cols-1 md:grid-cols-3 gap-6">
								{plansData
									.filter((plan) => plan.active)
									.map((plan) => (
										<PlanCard
											key={plan.key}
											plan={plan}
											isCurrent={currentPlan === plan.key}
											onRequest={handleRequest}
										/>
									))}
							</div>
						</div>
					)}
				</div>

				<div className="text-center text-xs text-muted-foreground mt-4">
					{t("pricingModal.agreementNotice.part1")}{" "}
					<a
						href={`${import.meta.env.VITE_APP_URL || "https://depthsight.pro"}/terms-of-service`}
						target="_blank"
						rel="noopener noreferrer"
						className="underline hover:text-primary"
					>
						{t("common:termsOfService")}
					</a>{" "}
					{t("pricingModal.agreementNotice.part2")}{" "}
					<a
						href={`${import.meta.env.VITE_APP_URL || "https://depthsight.pro"}/privacy-policy`}
						target="_blank"
						rel="noopener noreferrer"
						className="underline hover:text-primary"
					>
						{t("common:privacyPolicy")}
					</a>
					.
				</div>
			</DialogContent>
		</Dialog>
	);
};
