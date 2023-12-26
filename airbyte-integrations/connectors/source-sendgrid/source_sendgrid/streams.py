#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#

import datetime
import urllib
from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Union, List

import pendulum
import requests
from airbyte_cdk.sources.streams.http import HttpStream


class SendgridStream(HttpStream, ABC):
    url_base = "https://api.sendgrid.com/v3/"
    primary_key = "id"
    limit = 50
    data_field = None
    raise_on_http_errors = True
    permission_error_codes = {
        400: "authorization required",
        401: "authorization required",
    }

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        pass

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> Iterable[Mapping]:
        json_response = response.json()
        records = json_response.get(self.data_field, []) if self.data_field is not None else json_response

        if records is not None:
            for record in records:
                yield record
        else:
            # TODO sendgrid's API is sending null responses at times. This seems like a bug on the API side, so we're adding
            #  log statements to help reproduce and prevent the connector from failing.
            err_msg = (
                f"Response contained no valid JSON data. Response body: {response.text}\n"
                f"Response status: {response.status_code}\n"
                f"Response body: {response.text}\n"
                f"Response headers: {response.headers}\n"
                f"Request URL: {response.request.url}\n"
                f"Request body: {response.request.body}\n"
            )
            # do NOT print request headers as it contains auth token
            self.logger.info(err_msg)

    def should_retry(self, response: requests.Response) -> bool:
        """Override to provide skip the stream possibility"""

        status = response.status_code
        if status in self.permission_error_codes.keys():
            for message in response.json().get("errors", []):
                if message.get("message") == self.permission_error_codes.get(status):
                    self.logger.error(
                        f"Stream `{self.name}` is not available, due to subscription plan limitations or perrmission issues. Skipping."
                    )
                    setattr(self, "raise_on_http_errors", False)
                    return False
        elif status == 429:
            self.logger.info(f"Rate limit exceeded")
            setattr(self, "raise_on_http_errors", False)
            return True
        return 500 <= response.status_code < 600
    
    def backoff_time(self, response: requests.Response) -> float:
        # Not all responses have rate limit headers
        # Eg: Messages API has rate limit headers while lists API does not
        # If present the rate limit headers look like this (https://docs.sendgrid.com/api-reference/how-to-use-the-sendgrid-v3-api/rate-limits)
        # x-ratelimit-remaining: number of requests remaining
        # x-ratelimit-reset: time in seconds after which rate limit resets (Seems to be < 1 minute)
        # x-ratelimit-limit: total number of requests allowed in the current window
        response_headers = response.headers
        RATE_LIMIT_RESET_HEADER = "x-ratelimit-reset"
        if RATE_LIMIT_RESET_HEADER in response_headers:
            try:
                sleep_time = int(response_headers.get(RATE_LIMIT_RESET_HEADER)) + 1
                self.logger.info(f"Rate limit exceeded. Sleeping for {sleep_time} seconds")
                return sleep_time
            except ValueError:
                # Log error and use default exponential backoff
                self.logger.error(f"Rate limit reset header value is not a number. Value: {response_headers.get(RATE_LIMIT_RESET_HEADER)}")
        # Use default exponential backoff if rate limit headers are not present
        return super().backoff_time(response)


class SendgridStreamOffsetPagination(SendgridStream):
    offset = 0

    def request_params(self, next_page_token: Mapping[str, Any] = None, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(next_page_token=next_page_token, **kwargs)
        params["limit"] = self.limit
        if next_page_token:
            params.update(**next_page_token)
        return params

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        stream_data = response.json()
        if self.data_field:
            stream_data = stream_data[self.data_field]
        if len(stream_data) < self.limit:
            return
        self.offset += self.limit
        return {"offset": self.offset}


class SendgridStreamIncrementalMixin(HttpStream, ABC):
    cursor_field = "created"
    # Sendgrid API is returning data with latest cursor value first
    # So we are fetching data in slices and setting checkpoint after each slice 
    # Setting it too low would mean more API calls and possibility of rate limit breach => more wait time for rate limit resets
    slice_interval_in_minutes = 60

    def __init__(self, start_time: Optional[Union[int, str]], **kwargs):
        super().__init__(**kwargs)
        self._start_time = start_time or 0
        if isinstance(self._start_time, str):
            self._start_time = int(pendulum.parse(self._start_time).timestamp())

    def stream_slices(
        self, sync_mode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Optional[Mapping[str, Any]]]:
        stream_slices: list = []

        # use the latest date between self.start_date and stream_state
        start_date = self._start_time
        
        if stream_state and self.cursor_field and self.cursor_field in stream_state:
            # Some streams have a cursor field that is not a timestamp. So convert it to timestamp before comparing
            state_timestamp = stream_state[self.cursor_field]
            if not isinstance(state_timestamp, int):
                state_timestamp = pendulum.parse(state_timestamp).int_timestamp
            start_date = max(start_date, state_timestamp)

        end_date = int(pendulum.now().timestamp())

        while start_date <= end_date:
            current_end_date = int(pendulum.from_timestamp(start_date).add(minutes=self.slice_interval_in_minutes).timestamp())
            stream_slices.append(
                {
                    "start_date": start_date,
                    "end_date": min(current_end_date, end_date),
                }
            )
            # +1 because both dates are inclusive
            start_date = current_end_date + 1

        return stream_slices

    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        Return the latest state by comparing the cursor value in the latest record with the stream's most recent state object
        and returning an updated state object.
        """
        latest_benchmark = latest_record[self.cursor_field]
        if current_stream_state.get(self.cursor_field):
            return {self.cursor_field: max(latest_benchmark, current_stream_state[self.cursor_field])}
        return {self.cursor_field: latest_benchmark}

    def request_params(self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state, stream_slice, next_page_token)
        # To query data for one stream slice at a time 
        params.update({"start_time": stream_slice["start_date"], "end_time": stream_slice["end_date"]})
        return params


class SendgridStreamMetadataPagination(SendgridStream):
    def request_params(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:
        params = {}
        if not next_page_token:
            params = {"page_size": self.limit}
        return params

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        next_page_url = response.json()["_metadata"].get("next", False)
        if next_page_url:
            return {"next_page_url": next_page_url.replace(self.url_base, "")}

    @staticmethod
    @abstractmethod
    def initial_path() -> str:
        """
        :return: initial path for the API endpoint if no next metadata url found
        """

    def path(
        self,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> str:
        if next_page_token:
            return next_page_token["next_page_url"]
        return self.initial_path()


class Scopes(SendgridStream):
    def path(self, **kwargs) -> str:
        return "scopes"


class Lists(SendgridStreamMetadataPagination):
    data_field = "result"

    @staticmethod
    def initial_path() -> str:
        return "marketing/lists"


class Campaigns(SendgridStreamMetadataPagination):
    data_field = "result"

    @staticmethod
    def initial_path() -> str:
        return "marketing/campaigns"


class Contacts(SendgridStream):
    data_field = "result"

    def path(self, **kwargs) -> str:
        return "marketing/contacts"


class StatsAutomations(SendgridStreamMetadataPagination):
    data_field = "results"

    @staticmethod
    def initial_path() -> str:
        return "marketing/stats/automations"


class Segments(SendgridStream):
    data_field = "results"

    def path(self, **kwargs) -> str:
        return "marketing/segments"


class SingleSends(SendgridStreamMetadataPagination):
    """
    https://docs.sendgrid.com/api-reference/marketing-campaign-stats/get-all-single-sends-stats
    """

    data_field = "results"

    @staticmethod
    def initial_path() -> str:
        return "marketing/stats/singlesends"


class Templates(SendgridStreamMetadataPagination):
    data_field = "result"

    def request_params(self, next_page_token: Mapping[str, Any] = None, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(next_page_token=next_page_token, **kwargs)
        params["generations"] = "legacy,dynamic"
        return params

    @staticmethod
    def initial_path() -> str:
        return "templates"


class Messages(SendgridStreamOffsetPagination, SendgridStreamIncrementalMixin):
    """
    https://docs.sendgrid.com/api-reference/e-mail-activity/filter-all-messages
    """

    data_field = "messages"
    cursor_field = "last_event_time"
    primary_key = "msg_id"
    limit = 1000

    def request_params(self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None) -> str:
        time_filter_template = "%Y-%m-%dT%H:%M:%SZ"
        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice, next_page_token=next_page_token)
        if isinstance(params["start_time"], int):
            date_start = datetime.datetime.utcfromtimestamp(params["start_time"]).strftime(time_filter_template)
        else:
            date_start = params["start_time"]
        date_end = datetime.datetime.utcfromtimestamp(int(params["end_time"])).strftime(time_filter_template)
        queryapi = f'last_event_time BETWEEN TIMESTAMP "{date_start}" AND TIMESTAMP "{date_end}"'
        params["query"] = urllib.parse.quote(queryapi)
        params["limit"] = self.limit
        payload_str = "&".join("%s=%s" % (k, v) for k, v in params.items() if k not in ["start_time", "end_time"])
        return payload_str

    def path(self, stream_slice: Mapping[str, Any] = None, **kwargs) -> str:
        return "messages"


class GlobalSuppressions(SendgridStreamOffsetPagination, SendgridStreamIncrementalMixin):
    primary_key = "email"

    def path(self, **kwargs) -> str:
        return "suppression/unsubscribes"


class SuppressionGroups(SendgridStream):
    def path(self, **kwargs) -> str:
        return "asm/groups"


class SuppressionGroupMembers(SendgridStreamOffsetPagination):
    primary_key = ["group_id", "email"]

    def path(self, **kwargs) -> str:
        return "asm/suppressions"


class Blocks(SendgridStreamOffsetPagination, SendgridStreamIncrementalMixin):
    primary_key = "email"

    def path(self, **kwargs) -> str:
        return "suppression/blocks"


class Bounces(SendgridStreamOffsetPagination, SendgridStreamIncrementalMixin):
    primary_key = "email"

    def path(self, **kwargs) -> str:
        return "suppression/bounces"


class InvalidEmails(SendgridStreamOffsetPagination, SendgridStreamIncrementalMixin):
    primary_key = "email"

    def path(self, **kwargs) -> str:
        return "suppression/invalid_emails"


class SpamReports(SendgridStreamOffsetPagination, SendgridStreamIncrementalMixin):
    primary_key = "email"

    def path(self, **kwargs) -> str:
        return "suppression/spam_reports"
