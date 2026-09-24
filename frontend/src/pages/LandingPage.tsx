// src/pages/LandingPage.tsx

import { useEffect } from "react";
import { MarketingNav } from "@/components/marketing/MarketingNav";
import { Hero } from "@/components/marketing/Hero";
import { Features } from "@/components/marketing/Features";
import { Pricing } from "@/components/marketing/Pricing";
import { CTAFooter } from "@/components/marketing/CTAFooter";
import { useAuth } from "@/context/AuthContext";
import { Navigate } from "react-router-dom";

/**
 * Marketing landing page.
 *
 * Mounted at `/welcome` (public route). When an authenticated user
 * visits this URL, we redirect them straight to the dashboard so the
 * landing page is a pure marketing surface.
 *
 * Authenticated users who want to share DepthSight still have a
 * marketing URL they can hand to others — `/welcome` is always
 * accessible at any auth state (we just bounce authed users to the
 * dashboard).
 */
const LandingPage = () => {
	const { isAuthenticated, isLoading } = useAuth();

	useEffect(() => {
		document.title = "DepthSight — AI-driven crypto strategies";
	}, []);

	if (isLoading) return null;
	if (isAuthenticated) {
		return <Navigate to="/dashboard" replace />;
	}

	return (
		<div className="min-h-screen bg-background">
			<MarketingNav />
			<main>
				<Hero />
				<Features />
				<Pricing />
				<CTAFooter />
			</main>
		</div>
	);
};

export default LandingPage;
