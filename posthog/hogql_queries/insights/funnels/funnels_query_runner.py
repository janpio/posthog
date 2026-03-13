from datetime import datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from rest_framework.request import Request

from posthog.schema import (
    CachedFunnelsQueryResponse,
    FunnelsQuery,
    FunnelsQueryResponse,
    FunnelVizType,
    HogQLQueryModifiers,
    ResolvedDateRangeResponse,
)

from posthog.hogql import ast
from posthog.hogql.constants import MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY, HogQLGlobalSettings, LimitContext
from posthog.hogql.printer import to_printed_hogql
from posthog.hogql.query import execute_hogql_query
from posthog.hogql.timings import HogQLTimings

from posthog.caching.insights_api import BASE_MINIMUM_INSIGHT_REFRESH_INTERVAL, REDUCED_MINIMUM_INSIGHT_REFRESH_INTERVAL
from posthog.hogql_queries.insights.funnels import FunnelTrendsUDF, FunnelUDF
from posthog.hogql_queries.insights.funnels.funnel_query_context import FunnelQueryContext
from posthog.hogql_queries.insights.funnels.funnel_time_to_convert import FunnelTimeToConvertUDF
from posthog.hogql_queries.query_runner import AnalyticsQueryRunner
from posthog.hogql_queries.utils.query_date_range import QueryDateRange
from posthog.models import Team
from posthog.models.filters.mixins.utils import cached_property
from posthog.queries.breakdown_props import NOT_IN_COHORT_ID


class FunnelsQueryRunner(AnalyticsQueryRunner[FunnelsQueryResponse]):
    query: FunnelsQuery
    cached_response: CachedFunnelsQueryResponse
    context: FunnelQueryContext

    def __init__(
        self,
        query: FunnelsQuery | dict[str, Any],
        team: Team,
        timings: Optional[HogQLTimings] = None,
        modifiers: Optional[HogQLQueryModifiers] = None,
        limit_context: Optional[LimitContext] = None,
        request: Optional["Request"] = None,
        just_summarize: bool = False,
    ):
        super().__init__(
            query, team=team, timings=timings, modifiers=modifiers, limit_context=limit_context, request=request
        )

        self.just_summarize = just_summarize
        self.context = FunnelQueryContext(
            query=self.query, team=team, timings=timings, modifiers=modifiers, limit_context=limit_context
        )

    def _refresh_frequency(self):
        date_to = self.query_date_range.date_to()
        date_from = self.query_date_range.date_from()
        interval = self.query_date_range.interval_name

        delta_days: Optional[int] = None
        if date_from and date_to:
            delta = date_to - date_from
            delta_days = ceil(delta.total_seconds() / timedelta(days=1).total_seconds())

        refresh_frequency = BASE_MINIMUM_INSIGHT_REFRESH_INTERVAL
        if interval == "hour" or (delta_days is not None and delta_days <= 7):
            # The interval is shorter for short-term insights
            refresh_frequency = REDUCED_MINIMUM_INSIGHT_REFRESH_INTERVAL

        return refresh_frequency

    def to_query(self) -> ast.SelectQuery:
        return self.funnel_class.get_query()

    def to_actors_query(self) -> ast.SelectQuery:
        return self.funnel_actor_class.actor_query()

    def _calculate(self):
        query = self.to_query()
        timings = []

        # TODO: can we get this from execute_hogql_query as well?
        hogql = to_printed_hogql(query, self.team)

        response = execute_hogql_query(
            query_type="FunnelsQuery",
            query=query,
            team=self.team,
            timings=self.timings,
            modifiers=self.modifiers,
            limit_context=self.limit_context,
            settings=HogQLGlobalSettings(
                # Make sure funnel queries never OOM
                max_bytes_before_external_group_by=MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY,
                allow_experimental_analyzer=True,
            ),
        )

        results = self.funnel_class._format_results(response.results)
        results = self._ensure_not_in_cohort_group(results)

        if response.timings is not None:
            timings.extend(response.timings)

        return FunnelsQueryResponse(
            results=results,
            timings=timings,
            hogql=hogql,
            modifiers=self.modifiers,
            resolved_date_range=ResolvedDateRangeResponse(
                date_from=self.query_date_range.date_from(),
                date_to=self.query_date_range.date_to(),
            ),
        )

    @cached_property
    def funnel_order_class(self):
        return FunnelUDF(context=self.context)

    @cached_property
    def funnel_class(self):
        funnelVizType = self.context.funnelsFilter.funnelVizType

        if funnelVizType == FunnelVizType.TRENDS:
            return FunnelTrendsUDF(context=self.context, just_summarize=self.just_summarize)
        elif funnelVizType == FunnelVizType.TIME_TO_CONVERT:
            return FunnelTimeToConvertUDF(context=self.context)
        else:
            return self.funnel_order_class

    @cached_property
    def funnel_actor_class(self):
        if self.context.funnelsFilter.funnelVizType == FunnelVizType.TRENDS:
            return FunnelTrendsUDF(context=self.context)

        return FunnelUDF(context=self.context)

    def _should_add_not_in_cohort_group(self) -> bool:
        """Check if we need to add a 'not in cohort' group."""
        return self.funnel_class.should_add_not_in_cohort_group

    def _not_in_cohort_label(self) -> str:
        cohorts = self.funnel_class.breakdown_cohorts
        cohort_name = cohorts[0].name if cohorts else "cohort"
        return f"Not in {cohort_name}"

    def _ensure_not_in_cohort_group(self, results: Any) -> Any:
        """For single-cohort breakdowns, ensure the 'not in cohort' group is always
        present so the UI shows a clear binary split even when it is empty."""
        if not self._should_add_not_in_cohort_group():
            return results

        not_in_label = self._not_in_cohort_label()
        funnelVizType = self.context.funnelsFilter.funnelVizType

        if funnelVizType == FunnelVizType.TRENDS:
            return self._ensure_not_in_cohort_trends(results, not_in_label)
        elif funnelVizType == FunnelVizType.TIME_TO_CONVERT:
            return results
        else:
            return self._ensure_not_in_cohort_steps(results, not_in_label)

    def _ensure_not_in_cohort_steps(
        self, results: list[list[dict[str, Any]]], not_in_label: str
    ) -> list[list[dict[str, Any]]]:
        existing_breakdown_values = {
            step["breakdown_value"] for series in results for step in series if "breakdown_value" in step
        }
        if NOT_IN_COHORT_ID in existing_breakdown_values:
            return results

        empty_series: list[dict[str, Any]] = []
        for index, step in enumerate(self.context.query.series):
            serialized = self.funnel_class._serialize_step(step, 0, index)
            serialized.update(
                {
                    "average_conversion_time": None,
                    "median_conversion_time": None,
                    "breakdown": not_in_label,
                    "breakdown_value": NOT_IN_COHORT_ID,
                }
            )
            empty_series.append(serialized)
        results.append(empty_series)
        return results

    def _ensure_not_in_cohort_trends(self, results: list[dict[str, Any]], not_in_label: str) -> list[dict[str, Any]]:
        existing_breakdown_values = {r.get("breakdown_value") for r in results}
        if not_in_label in existing_breakdown_values:
            return results

        if not results:
            return results

        # Copy days/labels from existing result, fill data with zeros
        template = results[0]
        results.append(
            {
                "count": template.get("count", 0),
                "data": [0] * len(template.get("data", [])),
                "days": template.get("days", []),
                "labels": template.get("labels", []),
                "breakdown_value": not_in_label,
            }
        )
        return results

    @property
    def exact_timerange(self):
        return self.query.dateRange and self.query.dateRange.explicitDate

    @cached_property
    def query_date_range(self):
        return QueryDateRange(
            date_range=self.query.dateRange,
            team=self.team,
            interval=self.query.interval,
            now=datetime.now(),
            exact_timerange=self.exact_timerange,
        )
