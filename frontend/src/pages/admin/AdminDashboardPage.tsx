// src/pages/admin/AdminDashboardPage.tsx

import {
	Activity,
	CheckCircle2,
	Circle,
	Copy,
	ListChecks,
	Mail,
	Rocket,
	TrendingUp,
	UserPlus,
	Users,
} from "lucide-react";
import React from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, XAxis, YAxis } from "recharts";
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import {
	ChartContainer,
	ChartTooltip,
	ChartTooltipContent,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import {
	useAdminAffiliates,
	useAdminDashboardStats,
	useAdminGetUsers,
} from "@/lib/api";
import type { AdminUser } from "@/types/api";

const StatCard = ({
	title,
	value,
	icon,
	isLoading,
	description,
	trend,
}: {
	title: string;
	value: string | number;
	icon: React.ReactNode;
	isLoading: boolean;
	description?: string;
	trend?: { value: number; isPositive: boolean };
}) => (
	<Card>
		<CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
			<CardTitle className="text-sm font-medium">{title}</CardTitle>
			{icon}
		</CardHeader>
		<CardContent>
			{isLoading ? (
				<Skeleton className="h-8 w-24" />
			) : (
				<>
					<div className="text-2xl font-bold">{value}</div>
					{description && (
						<p className="text-xs text-muted-foreground mt-1">{description}</p>
					)}
					{trend && (
						<p
							className={`text-xs mt-1 flex items-center gap-1 ${trend.isPositive ? "text-green-600" : "text-red-600"}`}
						>
							{trend.isPositive ? "↑" : "↓"} {Math.abs(trend.value)}%
						</p>
					)}
				</>
			)}
		</CardContent>
	</Card>
);

const getDeterministicColor = (str: string) => {
	let hash = 0;
	for (let i = 0; i < str.length; i++) {
		hash = str.charCodeAt(i) + ((hash << 5) - hash);
	}
	const hue = Math.abs(hash) % 360;
	return `hsl(${hue}, 70%, 60%)`;
};

const OnboardingChecklist = ({ hasUsers }: { hasUsers: boolean }) => {
	const items = [
		{
			done: true,
			label: "Platform deployed",
			detail: "All 12 services running, ops sidecar monitoring",
		},
		{
			done: hasUsers,
			label: "First investor onboarded",
			detail: "Register a user at /register, or use the invite flow",
		},
		{
			done: false,
			label: "SMTP configured (password reset emails)",
			detail: "Set SMTP_* in .env and set EMAIL_CONFIRMATION_ENABLED=true",
		},
		{
			done: false,
			label: "AI provider key set (Co-Pilot functional)",
			detail: "QWEN_API_KEY or OPENROUTER_API_KEY in .env",
		},
		{
			done: false,
			label: "Slack/Telegram alerts wired to ops sidecar",
			detail: "Ship /opt/ops/state/alert to a webhook so you hear about issues",
		},
		{
			done: false,
			label: "Custom domain + branding",
			detail: "Update VITE_* in frontend/.env.production + pwa/.env.production",
		},
	];

	const completed = items.filter((i) => i.done).length;
	const pct = Math.round((completed / items.length) * 100);

	return (
		<Card>
			<CardHeader>
				<div className="flex items-start justify-between gap-4">
					<div>
						<CardTitle className="flex items-center gap-2">
							<Rocket className="h-5 w-5 text-primary" />
							Launch checklist
						</CardTitle>
						<CardDescription>
							You're {completed} of {items.length} steps into your launch. Tick these off and you're investor-ready.
						</CardDescription>
					</div>
					<div className="text-right shrink-0">
						<div className="text-3xl font-bold text-primary">{pct}%</div>
						<p className="text-xs text-muted-foreground">complete</p>
					</div>
				</div>
			</CardHeader>
			<CardContent>
				<div className="space-y-3">
					{items.map((item) => (
						<div
							key={item.label}
							className="flex items-start gap-3 p-3 rounded-lg border bg-card hover:bg-muted/30 transition-colors"
						>
							{item.done ? (
								<CheckCircle2 className="h-5 w-5 text-green-600 mt-0.5 shrink-0" />
							) : (
								<Circle className="h-5 w-5 text-muted-foreground/40 mt-0.5 shrink-0" />
							)}
							<div className="min-w-0">
								<p
									className={
										"text-sm font-medium " +
										(item.done ? "line-through text-muted-foreground" : "")
									}
								>
									{item.label}
								</p>
								<p className="text-xs text-muted-foreground">{item.detail}</p>
							</div>
						</div>
					))}
				</div>
			</CardContent>
		</Card>
	);
};

const EmptyState = ({
	icon: Icon,
	title,
	hint,
	cta,
}: {
	icon: React.ComponentType<{ className?: string }>;
	title: string;
	hint: string;
	cta?: string;
}) => (
	<div className="py-12 px-6 text-center">
		<div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-primary/10 mb-3">
			<Icon className="h-6 w-6 text-primary" />
		</div>
		<p className="text-sm font-medium mb-1">{title}</p>
		<p className="text-xs text-muted-foreground max-w-sm mx-auto">{hint}</p>
		{cta && (
			<p className="text-xs text-primary mt-2 font-medium">{cta}</p>
		)}
	</div>
);

const AdminDashboardPage: React.FC = () => {
	const { data: stats, isLoading: isLoadingStats } = useAdminDashboardStats();
	const { data: usersData, isLoading: isLoadingUsers } = useAdminGetUsers(
		1,
		1,
		undefined,
		undefined,
	);
	const { data: recentUsersData, isLoading: isLoadingRecentUsers } =
		useAdminGetUsers(1, 20, undefined, undefined);
	const { data: affiliatesData, isLoading: isLoadingAffiliates } =
		useAdminAffiliates(1, 1);

	const recentUsers = React.useMemo<AdminUser[]>(() => {
		if (!recentUsersData?.users) return [];
		return [...recentUsersData.users]
			.sort(
				(a, b) =>
					new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime(),
			)
			.slice(0, 5);
	}, [recentUsersData]);

	const chartData = React.useMemo(() => {
		const taskCounts = stats?.taskCountsByType;
		if (!taskCounts) return [];
		return Object.entries(taskCounts)
			.map(([type, count]) => ({
				name: type,
				count: count,
				fill: getDeterministicColor(type),
			}))
			.sort((a, b) => b.count - a.count);
	}, [stats]);

	const totalUsers = usersData?.total ?? 0;
	const totalAffiliates =
		affiliatesData?.users.filter((u) => (u.stats?.referralCount ?? 0) > 0)
			.length ?? 0;

	const formatRelativeTime = (iso: string) => {
		try {
			const diffMs = Date.now() - new Date(iso).getTime();
			const diffMin = Math.floor(diffMs / 60000);
			if (diffMin < 1) return "just now";
			if (diffMin < 60) return `${diffMin}m ago`;
			const diffH = Math.floor(diffMin / 60);
			if (diffH < 24) return `${diffH}h ago`;
			const diffD = Math.floor(diffH / 24);
			if (diffD < 30) return `${diffD}d ago`;
			return new Date(iso).toLocaleDateString();
		} catch {
			return "";
		}
	};

	return (
		<div className="space-y-6">
			<div>
				<h1 className="text-3xl font-bold mb-2">Dashboard</h1>
				<p className="text-muted-foreground">
					Overview of platform statistics and metrics
				</p>
			</div>

			{!isLoadingUsers && totalUsers <= 1 && (
				<OnboardingChecklist hasUsers={totalUsers > 1} />
			)}

			<div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
				<StatCard
					title="Total Users"
					value={totalUsers}
					icon={<Users className="h-4 w-4 text-blue-600" />}
					isLoading={isLoadingUsers}
					description="All registered users"
				/>
				<StatCard
					title="New Users (7d)"
					value={stats?.newUsersLast7Days ?? 0}
					icon={<UserPlus className="h-4 w-4 text-green-600" />}
					isLoading={isLoadingStats}
					description="Users in last 7 days"
				/>
				<StatCard
					title="Tasks Run (7d)"
					value={stats?.tasksRunLast7Days ?? 0}
					icon={<Activity className="h-4 w-4 text-purple-600" />}
					isLoading={isLoadingStats}
					description="Tasks executed recently"
				/>
				<StatCard
					title="Active Affiliates"
					value={totalAffiliates}
					icon={<TrendingUp className="h-4 w-4 text-orange-600" />}
					isLoading={isLoadingAffiliates}
					description="Affiliates with referrals"
				/>
			</div>

			<div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
				<Card className="lg:col-span-1">
					<CardHeader>
						<CardTitle>Task Distribution by Type</CardTitle>
						<CardDescription>
							Number of tasks executed by type in the last 7 days
						</CardDescription>
					</CardHeader>
					<CardContent>
						{isLoadingStats ? (
							<Skeleton className="h-[300px] w-full" />
						) : chartData.length > 0 ? (
							<ChartContainer
								config={{ count: { label: "Tasks" } }}
								className="h-[300px] w-full"
							>
								<BarChart
									data={chartData}
									margin={{ top: 5, right: 20, left: 0, bottom: 5 }}
								>
									<CartesianGrid strokeDasharray="3 3" vertical={false} />
									<XAxis
										dataKey="name"
										tickLine={false}
										tickMargin={10}
										axisLine={false}
										angle={-45}
										textAnchor="end"
										height={100}
									/>
									<YAxis />
									<ChartTooltip
										cursor={false}
										content={
											<ChartTooltipContent
												formatter={(value: unknown) => `${value} tasks`}
											/>
										}
									/>
									<Bar dataKey="count" radius={[8, 8, 0, 0]}>
										{chartData.map((entry, index) => (
											<Cell key={`cell-${index}`} fill={entry.fill} />
										))}
									</Bar>
								</BarChart>
							</ChartContainer>
						) : (
							<EmptyState
								icon={ListChecks}
								title="No tasks run yet"
								hint="Tasks appear here as users run backtests, deploy strategies, and trigger AI workflows."
								cta="Seed demo data → scripts/seed_demo_data.py"
							/>
						)}
					</CardContent>
				</Card>

				<Card className="lg:col-span-1">
					<CardHeader>
						<CardTitle>Quick Stats</CardTitle>
						<CardDescription>Additional platform metrics</CardDescription>
					</CardHeader>
					<CardContent className="space-y-4">
						<div className="flex justify-between items-center py-2 border-b">
							<span className="text-sm font-medium">
								Average Tasks per User
							</span>
							<span className="text-sm text-muted-foreground">
								{totalUsers > 0
									? ((stats?.tasksRunLast7Days ?? 0) / totalUsers).toFixed(2)
									: "0.00"}
							</span>
						</div>
						<div className="flex justify-between items-center py-2 border-b">
							<span className="text-sm font-medium">Total Task Types</span>
							<span className="text-sm text-muted-foreground">
								{Object.keys(stats?.taskCountsByType || {}).length}
							</span>
						</div>
						<div className="flex justify-between items-center py-2 border-b">
							<span className="text-sm font-medium">User Growth Rate</span>
							<span className="text-sm text-green-600">
								{totalUsers > 0
									? (
											((stats?.newUsersLast7Days ?? 0) / totalUsers) *
											100
										).toFixed(1)
									: "0.0"}
								%
							</span>
						</div>
						<div className="flex justify-between items-center py-2">
							<span className="text-sm font-medium">
								Affiliate Participation
							</span>
							<span className="text-sm text-muted-foreground">
								{totalUsers > 0
									? ((totalAffiliates / totalUsers) * 100).toFixed(1)
									: "0.0"}
								%
							</span>
						</div>
					</CardContent>
				</Card>
			</div>

			<div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
				<Card className="lg:col-span-2">
					<CardHeader>
						<CardTitle>Task Breakdown</CardTitle>
						<CardDescription>
							Detailed view of task execution by type
						</CardDescription>
					</CardHeader>
					<CardContent>
						{isLoadingStats ? (
							<Skeleton className="h-40 w-full" />
						) : stats?.taskCountsByType &&
							Object.keys(stats.taskCountsByType).length > 0 ? (
							<div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
								{Object.entries(stats.taskCountsByType)
									.sort(([, a], [, b]) => b - a)
									.map(([type, count]) => (
										<div
											key={type}
											className="flex items-center justify-between p-4 border rounded-lg hover:bg-muted/50 transition-colors"
										>
											<div className="flex items-center gap-3">
												<div className="p-2 rounded-full bg-primary/10">
													<ListChecks className="h-4 w-4 text-primary" />
												</div>
												<div>
													<p className="text-sm font-medium">{type}</p>
													<p className="text-xs text-muted-foreground">
														Task Type
													</p>
												</div>
											</div>
											<div className="text-right">
												<p className="text-lg font-bold">{count}</p>
												<p className="text-xs text-muted-foreground">executed</p>
											</div>
										</div>
									))}
							</div>
						) : (
							<EmptyState
								icon={ListChecks}
								title="No task activity yet"
								hint="When users run backtests, deploy strategies, or trigger AI workflows, they'll show up here."
							/>
						)}
					</CardContent>
				</Card>

				<Card>
					<CardHeader>
						<div className="flex items-start justify-between gap-2">
							<div>
								<CardTitle className="flex items-center gap-2">
									<UserPlus className="h-5 w-5 text-primary" />
									Recent signups
								</CardTitle>
								<CardDescription>
									Latest 5 users to join
								</CardDescription>
							</div>
							{recentUsers.length > 0 && (
								<Link
									to="/admin/users"
									className="text-xs text-primary hover:underline shrink-0"
								>
									View all
								</Link>
							)}
						</div>
					</CardHeader>
					<CardContent>
						{isLoadingRecentUsers ? (
							<div className="space-y-3">
								{[...Array(5)].map((_, i) => (
									<div key={i} className="flex items-center gap-3">
										<Skeleton className="h-9 w-9 rounded-full" />
										<div className="flex-1 space-y-1.5">
											<Skeleton className="h-3 w-24" />
											<Skeleton className="h-3 w-32" />
										</div>
									</div>
								))}
							</div>
						) : recentUsers.length > 0 ? (
							<div className="space-y-3">
								{recentUsers.map((user) => (
									<Link
										key={user.id}
										to={`/admin/users/${user.id}`}
										className="flex items-center gap-3 group hover:bg-muted/40 -mx-2 px-2 py-1.5 rounded-md transition-colors"
									>
										<div
											className="h-9 w-9 rounded-full flex items-center justify-center text-xs font-semibold text-white shrink-0"
											style={{
												backgroundColor: getDeterministicColor(
													user.username,
												),
											}}
										>
											{user.username.slice(0, 2).toUpperCase()}
										</div>
										<div className="min-w-0 flex-1">
											<div className="flex items-center gap-1.5">
												<p className="text-sm font-medium truncate group-hover:text-primary">
													{user.username}
												</p>
												{user.role === "admin" && (
													<span className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary font-medium uppercase tracking-wide">
														Admin
													</span>
												)}
											</div>
											<p className="text-xs text-muted-foreground truncate">
												{user.email}
											</p>
										</div>
										<div className="text-right shrink-0">
											<p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
												{user.plan}
											</p>
											<p className="text-[10px] text-muted-foreground/70">
												{formatRelativeTime(user.createdAt)}
											</p>
										</div>
									</Link>
								))}
							</div>
						) : (
							<EmptyState
								icon={UserPlus}
								title="No users yet"
								hint="As users register, the most recent five will show up here for quick triage."
							/>
						)}
					</CardContent>
				</Card>
			</div>
		</div>
	);
};

export default AdminDashboardPage;
