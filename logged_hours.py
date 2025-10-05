import requests
from requests.auth import HTTPBasicAuth
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import sys
import os

# Try to import configuration from jira_config.py
try:
    import jira_config as config
    
    # Validate that required config variables exist
    required_vars = [
        'JIRA_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN', 
        'REPORT_START_DATE', 'REPORT_END_DATE'
    ]
    
    missing_vars = [var for var in required_vars if not hasattr(config, var) or getattr(config, var) == ""]
    
    if missing_vars:
        print(f"Error: Missing required configuration variables: {', '.join(missing_vars)}")
        print("Please update the jira_config.py file with your settings.")
        sys.exit(1)
    
except ImportError:
    print("Error: Could not find jira_config.py file.")
    print("Please ensure jira_config.py is in the same directory as this script.")
    sys.exit(1)

# Load configuration
base_url = config.JIRA_URL
email = config.JIRA_EMAIL
api_token = config.JIRA_API_TOKEN
project_keys = getattr(config, 'PROJECT_KEYS', [])

# File output options
save_report_to_file = getattr(config, 'SAVE_REPORT_TO_FILE', True)
report_file_path = getattr(config, 'REPORT_FILE_PATH', "reports/")

# We don't use story points - just time estimates

# Authentication setup
auth = HTTPBasicAuth(email, api_token)
headers = {
   "Accept": "application/json",
   "Content-Type": "application/json"
}


def main():
    """Main function to run the simplified Jira time report"""
    print("\nJIRA TIME REPORT - ACTUALS vs ESTIMATES")
    print("=" * 60)
    
    # Parse the reporting period dates
    start_date, end_date = parse_date_range(config.REPORT_START_DATE, config.REPORT_END_DATE)
    
    print(f"Period: {start_date.date()} to {end_date.date()}")
    print(f"Projects: {', '.join(project_keys) if project_keys else 'All projects'}")
    print("=" * 60)
    
    # Build JQL query and fetch issues
    jql = build_jql_query(project_keys, start_date, end_date)
    issues = fetch_all_issues(jql)
    
    # Process worklogs and get actuals
    user_hours, user_names, issue_hours, user_issue_hours = process_worklogs(issues, start_date, end_date)
    
    # Calculate user estimates based on tickets they worked on
    user_estimates = calculate_user_estimates(user_issue_hours, issues)
    
    # Generate report
    report_text = generate_report(issues, user_hours, user_names, issue_hours, user_issue_hours, user_estimates)
    
    # Print report
    print(report_text)
    
    # Save report to file if enabled
    if save_report_to_file:
        save_report(report_text, start_date, end_date)


def parse_date_range(start_date_str, end_date_str):
    """Parse and validate the report date range"""
    try:
        start_date = datetime.fromisoformat(start_date_str)
        end_date = datetime.fromisoformat(end_date_str)
        
        # Ensure dates have timezone info
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        
        # Make end_date inclusive if it's set to midnight
        if end_date.hour == 0 and end_date.minute == 0 and end_date.second == 0:
            end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        return start_date, end_date
        
    except ValueError as e:
        print(f"Error parsing report dates: {e}")
        print("Please use ISO format: YYYY-MM-DDThh:mm:ss±hh:mm")
        sys.exit(1)


def build_jql_query(project_keys, start_date, end_date):
    """Build the JQL query to find relevant issues"""
    jql_parts = []

    # Add project filter if specified
    if project_keys:
        if len(project_keys) == 1:
            jql_parts.append(f"project = {project_keys[0]}")
        else:
            jql_parts.append(f"project in ({','.join(project_keys)})")

    # Add date range filter for worklogs
    start_date_str = start_date.strftime("%Y-%m-%d")
    end_date_str = end_date.strftime("%Y-%m-%d")
    
    date_clause = f"worklogDate >= '{start_date_str}' AND worklogDate <= '{end_date_str}'"
    jql_parts.append(f"({date_clause})")

    # Combine JQL parts
    jql = " AND ".join(jql_parts)
    print(f"JQL query: {jql}")
    
    return jql


def fetch_all_issues(jql):
    """Fetch all issues matching the JQL query"""
    issues_url = f"{base_url}/rest/api/3/search/jql"
    
    # Include fields needed for the report
    fields = [
        "key", "summary", "issuetype", "assignee", 
        "timeoriginalestimate", "timeestimate", "timespent"
    ]
    
    issues_query = {
        "jql": jql,
        "fields": fields,
        "maxResults": 100
    }

    print(f"Fetching issues...")
    all_issues = []
    start_at = 0
    total = None

    while total is None or start_at < total:
        
        response = requests.post(
            issues_url,
            headers=headers,
            auth=auth,
            json=issues_query
        )
        
        if response.status_code != 200:
            print(f"Error fetching issues: {response.status_code}")
            print(response.text)
            sys.exit(1)
            
        data = response.json()
        
        if total is None:
            total = data["total"]
            print(f"Found {total} issues with worklogs in the period")
        
        all_issues.extend(data["issues"])
        start_at += len(data["issues"])
        
        if len(data["issues"]) == 0:
            break

    print(f"Fetched {len(all_issues)} issues")
    return all_issues


def process_worklogs(issues, start_date, end_date):
    """Process worklogs for all issues"""
    user_hours = defaultdict(float)  # Total hours per user
    user_names = {}  # User display names
    issue_hours = {}  # Total hours per issue
    user_issue_hours = defaultdict(lambda: defaultdict(float))  # Hours per user per issue
    
    print(f"\nProcessing worklogs...")
    
    for i, issue in enumerate(issues):
        issue_key = issue["key"]
        
        # Fetch worklogs for this issue
        worklog_url = f"{base_url}/rest/api/3/issue/{issue_key}/worklog"
        
        response = requests.get(worklog_url, headers=headers, auth=auth)
        
        if response.status_code != 200:
            print(f"Error fetching worklogs for {issue_key}: {response.status_code}")
            continue
        
        worklog_data = response.json()
        issue_total_hours = 0
        
        # Process each worklog entry
        for worklog in worklog_data["worklogs"]:
            # Parse worklog date
            try:
                worklog_started = datetime.fromisoformat(worklog["started"].replace('Z', '+00:00'))
            except ValueError:
                continue
            
            # Check if worklog is in our date range
            if start_date <= worklog_started <= end_date:
                user_id = worklog["author"]["accountId"]
                user_name = worklog["author"]["displayName"]
                hours = worklog["timeSpentSeconds"] / 3600
                
                user_hours[user_id] += hours
                user_names[user_id] = user_name
                user_issue_hours[user_id][issue_key] += hours
                issue_total_hours += hours
        
        if issue_total_hours > 0:
            issue_hours[issue_key] = issue_total_hours
        
        if (i + 1) % 10 == 0 or (i + 1) == len(issues):
            print(f"Processed {i + 1}/{len(issues)} issues")

    return user_hours, user_names, issue_hours, user_issue_hours


def calculate_user_estimates(user_issue_hours, issues):
    """Calculate total estimated hours for each user based on tickets they worked on"""
    user_estimates = defaultdict(float)
    issue_lookup = {issue["key"]: issue for issue in issues}
    
    # For each user, sum up the original estimates for tickets they worked on
    for user_id, tickets in user_issue_hours.items():
        for issue_key in tickets.keys():
            if issue_key in issue_lookup:
                issue = issue_lookup[issue_key]
                original_estimate = get_estimate_hours(issue["fields"].get("timeoriginalestimate"))
                if original_estimate > 0:
                    # Add the full estimate to the user's total
                    # Note: This means if multiple people work on the same ticket,
                    # the estimate is counted for each person
                    user_estimates[user_id] += original_estimate
    
    return user_estimates


def get_estimate_hours(time_seconds):
    """Convert time estimate from seconds to hours"""
    if time_seconds:
        return time_seconds / 3600
    return 0


def get_story_points(issue_fields):
    """We don't use story points - this function is removed"""
    return None


def format_time(hours):
    """Format hours as hours and days"""
    if hours == 0:
        return "0h"
    
    days = hours / 8  # Assuming 8-hour work days
    if days >= 1:
        return f"{hours:.1f}h ({days:.1f}d)"
    else:
        return f"{hours:.1f}h"


def generate_report(issues, user_hours, user_names, issue_hours, user_issue_hours, user_estimates):
    """Generate the report text and return it"""
    
    # Create issue lookup for easier access
    issue_lookup = {issue["key"]: issue for issue in issues}
    
    report_lines = []

    report_lines.append("=" * 80)
    report_lines.append("ACTUALS vs ESTIMATES BY TEAM MEMBER")
    report_lines.append("=" * 80)
    report_lines.append(f"{'Team Member':<30} {'Est Hours':<15} {'Act Hours':<15} {'Variance':<15} {'Tickets':<10}")
    report_lines.append("-" * 80)
    
    # Count tickets per user
    user_ticket_counts = {user_id: len(tickets) for user_id, tickets in user_issue_hours.items()}
    
    # Sort users by hours logged (descending)
    total_estimated_all_users = 0
    total_actual_all_users = 0
    
    for user_id, hours in sorted(user_hours.items(), key=lambda x: x[1], reverse=True):
        user_name = user_names.get(user_id, user_id)
        ticket_count = user_ticket_counts.get(user_id, 0)
        estimated = user_estimates.get(user_id, 0)
        variance = hours - estimated
        variance_str = f"{variance:+.1f}h" if estimated > 0 else "N/A"
        
        total_estimated_all_users += estimated
        total_actual_all_users += hours
        
        report_lines.append(f"{user_name:<30} {format_time(estimated):<15} {format_time(hours):<15} {variance_str:<15} {ticket_count:<10}")
    
    report_lines.append("-" * 80)
    total_variance = total_actual_all_users - total_estimated_all_users
    total_variance_str = f"{total_variance:+.1f}h" if total_estimated_all_users > 0 else "N/A"
    report_lines.append(f"{'TOTAL':<30} {format_time(total_estimated_all_users):<15} {format_time(total_actual_all_users):<15} {total_variance_str:<15} {len(issue_hours):<10}")
    
    report_lines.append(f"\nTeam Members: {len(user_hours)}")
    report_lines.append(f"Average Estimated Hours per Person: {format_time(total_estimated_all_users / len(user_hours)) if user_hours else '0h'}")
    report_lines.append(f"Average Actual Hours per Person: {format_time(total_actual_all_users / len(user_hours)) if user_hours else '0h'}")
    
    if total_estimated_all_users > 0:
        accuracy = (1 - abs(total_actual_all_users - total_estimated_all_users) / total_estimated_all_users) * 100
        report_lines.append(f"Overall Estimation Accuracy: {accuracy:.1f}%")
    
    report_lines.append("\n" + "=" * 70)
    report_lines.append("ACTUALS vs ESTIMATES BY TICKET")
    report_lines.append("=" * 70)
    report_lines.append(f"{'Ticket':<12} {'Summary':<35} {'Original Est':<12} {'Actual':<12} {'Variance':<12}")
    report_lines.append("-" * 70)
    
    total_estimated = 0
    total_actual = 0
    tickets_with_estimates = 0

    # Sort issues by key for consistent output
    for issue_key in sorted(issue_hours.keys()):
        if issue_key in issue_lookup:
            issue = issue_lookup[issue_key]
            fields = issue["fields"]
            
            # Get estimates and actuals
            original_estimate = get_estimate_hours(fields.get("timeoriginalestimate"))
            actual_hours = issue_hours[issue_key]
            
            # Calculate variance
            if original_estimate > 0:
                variance = actual_hours - original_estimate
                variance_str = f"{variance:+.1f}h"
                total_estimated += original_estimate
                tickets_with_estimates += 1
            else:
                variance_str = "N/A"
            
            total_actual += actual_hours
            
            # Truncate summary if too long
            summary = fields["summary"][:32] + "..." if len(fields["summary"]) > 35 else fields["summary"]
            
            report_lines.append(f"{issue_key:<12} {summary:<35} {format_time(original_estimate):<12} "
                              f"{format_time(actual_hours):<12} {variance_str:<12}")
    
    report_lines.append("-" * 70)
    report_lines.append(f"{'TOTALS':<12} {'':<35} {format_time(total_estimated):<12} "
                       f"{format_time(total_actual):<12} {format_time(total_actual - total_estimated):+<12}")
    
    if tickets_with_estimates > 0:
        accuracy = (1 - abs(total_actual - total_estimated) / total_estimated) * 100
        report_lines.append(f"\nEstimation Accuracy: {accuracy:.1f}%")
        report_lines.append(f"Tickets with Estimates: {tickets_with_estimates}/{len(issue_hours)}")
    
    # SECTION: Breakdown of actuals for each team member across stories
    report_lines.append("\n" + "=" * 90)
    report_lines.append("BREAKDOWN OF ACTUALS BY TEAM MEMBER ACROSS STORIES")
    report_lines.append("=" * 90)
    
    for user_id, hours in sorted(user_hours.items(), key=lambda x: x[1], reverse=True):
        user_name = user_names.get(user_id, user_id)
        user_total_estimate = user_estimates.get(user_id, 0)
        report_lines.append(f"\n{user_name} - Estimated: {format_time(user_total_estimate)} | Actual: {format_time(hours)}")
        report_lines.append("-" * 70)
        report_lines.append(f"{'Ticket':<12} {'Estimated':<12} {'Actual':<12} {'Summary':<30}")
        report_lines.append("-" * 70)
        
        # Sort this user's tickets by hours (descending)
        user_tickets = sorted(user_issue_hours[user_id].items(), key=lambda x: x[1], reverse=True)
        
        for issue_key, issue_hours_for_user in user_tickets:
            if issue_key in issue_lookup:
                issue = issue_lookup[issue_key]
                summary = issue["fields"]["summary"]
                original_estimate = get_estimate_hours(issue["fields"].get("timeoriginalestimate"))
                
                # Truncate summary for display
                summary = summary[:27] + "..." if len(summary) > 30 else summary
                report_lines.append(f"{issue_key:<12} {format_time(original_estimate):<12} {format_time(issue_hours_for_user):<12} {summary}")
    
    # SECTION: For each ticket, breakdown of actuals per member
    report_lines.append("\n" + "=" * 90)
    report_lines.append("BREAKDOWN OF ACTUALS BY TICKET ACROSS TEAM MEMBERS")
    report_lines.append("=" * 90)
    
    # Create reverse mapping: issue -> users who worked on it
    issue_user_hours = defaultdict(dict)
    for user_id, tickets in user_issue_hours.items():
        for issue_key, hours in tickets.items():
            issue_user_hours[issue_key][user_id] = hours
    
    # Sort issues by total hours (descending)
    for issue_key in sorted(issue_hours.keys(), key=lambda x: issue_hours[x], reverse=True):
        if issue_key in issue_lookup:
            issue = issue_lookup[issue_key]
            summary = issue["fields"]["summary"]
            total_hours = issue_hours[issue_key]
            original_estimate = get_estimate_hours(issue["fields"].get("timeoriginalestimate"))
            
            report_lines.append(f"\n{issue_key} - Estimated: {format_time(original_estimate)} | Actual: {format_time(total_hours)}")
            report_lines.append(f"Summary: {summary}")
            report_lines.append("-" * 70)
            report_lines.append(f"{'Team Member':<30} {'Hours':<12} {'Percentage':<12}")
            report_lines.append("-" * 70)
            
            # Sort users by their hours on this ticket (descending)
            ticket_users = sorted(issue_user_hours[issue_key].items(), key=lambda x: x[1], reverse=True)
            
            for user_id, user_hours_on_ticket in ticket_users:
                user_name = user_names.get(user_id, user_id)
                percentage = (user_hours_on_ticket / total_hours) * 100
                report_lines.append(f"{user_name:<30} {format_time(user_hours_on_ticket):<12} {percentage:.1f}%")
    
    return "\n".join(report_lines)



def save_report(report_text, start_date, end_date):
    """Save the report to a file"""
    # Create the reports directory if it doesn't exist
    if not os.path.exists(report_file_path):
        os.makedirs(report_file_path)
    
    # Format the filename with date range
    project_suffix = "_".join(project_keys) if project_keys else "AllProjects"
    filename = f"Jira_Time_Report_{project_suffix}_{start_date.date()}_to_{end_date.date()}.txt"
    
    # Full path to the file
    file_path = os.path.join(report_file_path, filename)
    
    # Write the report to the file
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    print(f"\n" + "=" * 50)
    print(f"REPORT SAVED TO FILE")
    print("=" * 50)
    print(f"File: {file_path}")
    print(f"Size: {os.path.getsize(file_path)} bytes")


if __name__ == "__main__":
    main()
