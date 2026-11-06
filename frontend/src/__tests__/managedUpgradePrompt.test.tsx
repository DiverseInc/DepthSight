// @vitest-environment jsdom
// src/__tests__/managedUpgradePrompt.test.tsx
//
// THE DEFECT THIS PINS
// =====================
// The $499 "Managed" tier sits ABOVE Professional ("pro"). Before this tier
// existed, "pro" WAS the top tier, so `user.plan !== "pro"` was a correct
// question to ask: "is this customer still owed an upgrade prompt?". The
// tier above it made that question wrong, and a customer who paid $499 was
// told to upgrade, four times over:
//
//   * the "Upgrade to PRO" entry in the user menu (UserNav.tsx);
//   * the amber upgrade styling on the plan badge (AppSidebar.tsx);
//   * the amber crown on the plan badge (AppSidebar.tsx);
//   * the "Upgrade plan" tooltip on the plan badge (AppSidebar.tsx).
//
// THE LEGACY KEY IS NOT OPTIONAL
// ==============================
// `premium` is the legacy spelling of the Managed tier. The server resolves
// it AT READ TIME (api/plans.py `resolve_plan_key`), but the rows are NOT
// migrated -- it was shipped as an alias, not as a data migration, and
// tests/test_managed_499_tier.py pins that the live `premium` users stay
// `premium`. The frontend therefore sees the RAW key:
// `/users/me` returns the ORM user (api/depthsight_api.py:1874), the login
// response is `schemas.User.model_validate(user_db)` (api/routes/auth.py:187)
// and `User.plan` is a plain string. So "managed" and "premium" are BOTH
// keys the frontend really receives, and a fix that only knew "managed"
// would still nag a legacy customer.
//
// These tests assert what the CUSTOMER SEES -- the rendered menu entry, the
// rendered crown, the rendered badge styling, the rendered tooltip -- never
// an internal helper.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18n from "i18next";
import type React from "react";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import accountEn from "@/locales/en/account.json";
import commonEn from "@/locales/en/common.json";
import navigationEn from "@/locales/en/navigation.json";

interface MockUser {
	id: number;
	username: string;
	email: string;
	plan: string;
	role: string;
}

// The signed-in customer. The plan is swapped per test.
const authMock = vi.hoisted(() => ({
	user: {
		id: 1,
		username: "tester",
		email: "tester@example.com",
		plan: "free",
		role: "user",
	} as MockUser,
	isLoading: false,
	logout: () => undefined,
}));

vi.mock("@/context/AuthContext", () => ({
	useAuth: () => authMock,
}));

vi.mock("@/lib/api", () => ({
	useAccountStatus: () => ({
		data: {
			planName: authMock.user.plan,
			planExpiresAt: null,
			quotas: [],
		},
		isLoading: false,
	}),
	useSystemStatus: () => ({ data: undefined }),
}));

import { AppSidebar } from "@/components/AppSidebar";
import { UserNav } from "@/components/UserNav";
import { SidebarProvider } from "@/components/ui/sidebar";

const wrapper = ({ children }: { children: React.ReactNode }) => (
	<MemoryRouter>{children}</MemoryRouter>
);

/** The plan badge in the sidebar: the link to /account that carries the tier. */
const planBadgeLink = (container: HTMLElement): HTMLElement => {
	const link = container.querySelector<HTMLElement>('a[href="/account"]');
	if (!link) throw new Error("the sidebar plan badge was not rendered");
	return link;
};

/** The upgrade crown is the only icon inside the plan badge. */
const crownCount = (badge: HTMLElement): number =>
	badge.querySelectorAll(".lucide-crown").length;

/** Open the plan badge tooltip and return the text the customer is shown. */
const planTooltipText = async (container: HTMLElement): Promise<string> => {
	const trigger = planBadgeLink(container);
	fireEvent.focusIn(trigger);
	await waitFor(() =>
		expect(screen.queryByText(/Current Plan|Upgrade plan/)).toBeTruthy(),
	);
	const tooltip = screen.getByText(/Current Plan|Upgrade plan/);
	return tooltip.textContent ?? "";
};

/**
 * Open the user menu, the way a customer opens it.
 *
 * The menu content lives in a portal and is only rendered while the menu is
 * open, so the customer has to open it to see the upgrade entry.
 */
const openUserMenu = async (container: HTMLElement) => {
	const trigger = container.querySelector("button");
	if (!trigger) throw new Error("the user menu trigger was not rendered");
	fireEvent.keyDown(trigger, { key: "Enter" });
	await waitFor(() => expect(screen.queryByRole("menu")).toBeTruthy());
};

/** The user menu, as the customer sees it. */
const renderUserNav = () => render(<UserNav />, { wrapper });

/** The sidebar, as the customer sees it. */
const renderSidebar = () =>
	render(
		<SidebarProvider>
			<AppSidebar />
		</SidebarProvider>,
		{ wrapper },
	);

beforeAll(async () => {
	// jsdom has neither of these, and the radix controls the sidebar uses
	// (tooltip, dropdown) need them to render at all.
	if (!("ResizeObserver" in globalThis)) {
		globalThis.ResizeObserver = class {
			observe() {}
			unobserve() {}
			disconnect() {}
		} as unknown as typeof ResizeObserver;
	}
	if (!Element.prototype.hasPointerCapture) {
		Element.prototype.hasPointerCapture = () => false;
	}
	if (!Element.prototype.releasePointerCapture) {
		Element.prototype.releasePointerCapture = () => undefined;
	}
	// jsdom has no matchMedia, and the sidebar asks it whether the viewport
	// is a phone.
	if (!window.matchMedia) {
		window.matchMedia = ((query: string) => ({
			matches: false,
			media: query,
			onchange: null,
			addListener: () => undefined,
			removeListener: () => undefined,
			addEventListener: () => undefined,
			removeEventListener: () => undefined,
			dispatchEvent: () => false,
		})) as unknown as typeof window.matchMedia;
	}

	await i18n.use(initReactI18next).init({
		resources: {
			en: {
				common: commonEn,
				navigation: navigationEn,
				account: accountEn,
			},
		},
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

beforeEach(() => {
	authMock.user.plan = "free";
	// The sidebar asks the hub for its version on mount. jsdom has no
	// network; the component ignores the failure.
	vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe("the $499 Managed tier is never told to upgrade", () => {
	it("shows a Managed customer no upgrade prompt at all", async () => {
		authMock.user.plan = "managed";

		// 1. The "Upgrade to PRO" entry in the user menu.
		const nav = renderUserNav();
		await openUserMenu(nav.container);
		expect(screen.queryByText(/upgrade to pro/i)).toBeNull();
		nav.unmount();

		// 2. The amber upgrade styling and the crown on the plan badge.
		const sidebar = renderSidebar();
		const badge = planBadgeLink(sidebar.container);
		expect(badge.querySelector("div")?.className).not.toContain(
			"hover:bg-amber-500/10",
		);
		expect(crownCount(badge)).toBe(0);

		// 3. The tooltip says which plan they are on, not "Upgrade plan".
		expect(await planTooltipText(sidebar.container)).toBe(
			"Current Plan: managed",
		);
		expect(screen.queryByText(/upgrade plan/i)).toBeNull();
	});

	it("shows a legacy premium customer no upgrade prompt either", async () => {
		// `premium` is the legacy spelling of Managed and the frontend sees
		// the raw key, so the prompt must not come back for these rows.
		authMock.user.plan = "premium";

		const nav = renderUserNav();
		await openUserMenu(nav.container);
		expect(screen.queryByText(/upgrade to pro/i)).toBeNull();
		nav.unmount();

		const sidebar = renderSidebar();
		const badge = planBadgeLink(sidebar.container);
		expect(badge.querySelector("div")?.className).not.toContain(
			"hover:bg-amber-500/10",
		);
		expect(crownCount(badge)).toBe(0);
		expect(await planTooltipText(sidebar.container)).toBe(
			"Current Plan: premium",
		);
		expect(screen.queryByText(/upgrade plan/i)).toBeNull();
	});

	it("still shows no upgrade prompt to a Professional customer", async () => {
		// No regression: "pro" is not the top tier any more, but it was
		// never shown an upgrade prompt, so it must not start now.
		authMock.user.plan = "pro";

		const nav = renderUserNav();
		await openUserMenu(nav.container);
		expect(screen.queryByText(/upgrade to pro/i)).toBeNull();
		nav.unmount();

		const sidebar = renderSidebar();
		const badge = planBadgeLink(sidebar.container);
		expect(badge.querySelector("div")?.className).not.toContain(
			"hover:bg-amber-500/10",
		);
		expect(crownCount(badge)).toBe(0);
		expect(await planTooltipText(sidebar.container)).toBe("Current Plan: pro");
		expect(screen.queryByText(/upgrade plan/i)).toBeNull();
	});

	it("still shows the upgrade prompt to a free customer", async () => {
		authMock.user.plan = "free";

		const nav = renderUserNav();
		await openUserMenu(nav.container);
		expect(screen.getByText(/upgrade to pro/i)).toBeTruthy();
		nav.unmount();

		const sidebar = renderSidebar();
		const badge = planBadgeLink(sidebar.container);
		expect(badge.querySelector("div")?.className).toContain(
			"hover:bg-amber-500/10",
		);
		expect(crownCount(badge)).toBe(1);
		expect(await planTooltipText(sidebar.container)).toBe("Upgrade plan");
	});

	it("still shows the upgrade prompt to a Standard customer", async () => {
		// A paying customer who is not on the top tier is still a customer
		// the platform can sell to.
		authMock.user.plan = "standard";

		const nav = renderUserNav();
		await openUserMenu(nav.container);
		expect(screen.getByText(/upgrade to pro/i)).toBeTruthy();
		nav.unmount();

		const sidebar = renderSidebar();
		const badge = planBadgeLink(sidebar.container);
		expect(badge.querySelector("div")?.className).toContain(
			"hover:bg-amber-500/10",
		);
		expect(crownCount(badge)).toBe(1);
		expect(await planTooltipText(sidebar.container)).toBe("Upgrade plan");
	});
});
