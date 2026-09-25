from github import Github, GithubException
from google import genai
from google.genai.errors import APIError
import os
import re
import csv
import json
import sys
import time
from getpass import getpass
import requests

GEMINI_MODEL = 'gemini-2.5-flash'
MAX_DIFF_CHARS = 50000
OUTPUT_FILENAME = "results.csv"
PR_SLEEP_TIME = 1

def get_user_inputs():
    print("--- Gemini Pull Request Summarizer ---")

    repo_url = input("Enter the GitHub Repo URL: ")

    # Secrets are read from environment variables first; if missing, they are
    # typed in without being shown on screen.
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        github_token = getpass("Enter Your GitHub Token (input hidden): ")

    while True:
        try:
            n_prs = int(input("Enter the Number of PRs to summarize and generate a title for: "))
            if n_prs > 0:
                break
            else:
                print("N must be a positive integer.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        print("\nWARNING: GEMINI_API_KEY environment variable is not set.")
        gemini_api_key = getpass("Enter your Gemini API Key (input hidden): ")

    return repo_url, github_token, n_prs, gemini_api_key

def get_latest_merged_prs(repo_url, github_token, n_prs):
    try:
        path = '/'.join(repo_url.strip('/').split('/')[-2:])

        g = Github(github_token)

        repo = g.get_repo(path)

        print(f"\nAttempting to fetch PRs from: {repo.full_name}")

        all_prs = repo.get_pulls(state='closed', sort='updated', direction='desc')

        merged_prs = []
        for pr in all_prs:
            if pr.merged:
                merged_prs.append(pr)
                if len(merged_prs) >= n_prs:
                    break

        if not merged_prs:
            print("ERROR: No merged PRs found matching the criteria.")

        return merged_prs, repo

    except GithubException as e:
        print(f"\nGitHub API Error (Check token/URL/permissions): {e}")
        return [], None
    except Exception as e:
        print(f"\nAn unexpected error occurred during GitHub access: {e}")
        return [], None

def clean_pr_description(body):
    """Remove hidden HTML comments (e.g. PR template instructions) from a PR description."""
    cleaned = re.sub(r"<!--.*?-->", "", body or "", flags=re.DOTALL).strip()
    return cleaned or "No description"

def get_diff_content_via_http(repo_owner, repo_name, pr_number, github_token):
    headers = {
        'Authorization': f'token {github_token}',
        'Accept': 'application/vnd.github.v3.diff'
    }
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pr_number}"

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return response.text
    else:
        raise Exception(f"HTTP Error {response.status_code}. Response: {response.text}")

def generate_summary(content, gemini_api_key):
    prompt = f"""
    The following text contains the code difference (diff) and the description of a GitHub Pull Request.

    Your task is to:
    1. Generate a **short and effective title (Generated_Title)** describing the PR's purpose and changes.
    2. Generate a **concise and technical summary (Generated_Summary)** detailing the purpose and the key code changes.

    Respond ONLY with the following JSON format (do not include any other text):
    {{
      "Generated_Title": "The new title you create",
      "Generated_Summary": "The new summary you create"
    }}

    PR Content (Description and Diff):
    ---
    {content}
    ---
    """

    try:
        client = genai.Client(api_key=gemini_api_key)

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "object",
                    "properties": {
                        "Generated_Title": {"type": "string"},
                        "Generated_Summary": {"type": "string"}
                    },
                    "required": ["Generated_Title", "Generated_Summary"]
                }
            }
        )

        return json.loads(response.text)

    except APIError as e:
        print(f"   -> Gemini API Error: {e}")
        return None
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"   -> Failed to parse Gemini response as JSON: {e}")
        return None
    except Exception as e:
        print(f"   -> Unexpected error during summarization: {e}")
        return None

def save_results_to_csv(results, filename):
    if not results:
        print("No results to save.")
        return

    fieldnames = [
        "PR #",
        "Original PR Title",
        "Generated PR Title",
        "Original PR Summary",
        "Generated PR Summary"
    ]

    try:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            writer.writerows(results)

        print(f"\nSUCCESS! Results successfully saved to '{filename}'.")

    except Exception as e:
        print(f"\nCSV saving error: {e}")

if __name__ == "__main__":

    repo_url, github_token, n_prs, gemini_api_key = get_user_inputs()

    merged_prs, repo = get_latest_merged_prs(repo_url, github_token, n_prs)

    if not merged_prs or repo is None:
        print("Program terminated. Could not retrieve pull requests or repository details.")
        sys.exit(1)

    repo_path_parts = repo.full_name.split('/')
    repo_owner = repo_path_parts[0]
    repo_name = repo_path_parts[1]

    results = []
    print(f"\nStarting summarization for {len(merged_prs)} PRs using {GEMINI_MODEL}...")

    for pr in merged_prs:
        print(f"Processing -> PR #{pr.number} (Original Title: {pr.title})")

        try:
            full_pr_details = repo.get_pull(pr.number)
            original_summary_full = clean_pr_description(full_pr_details.body)

            diff_text = get_diff_content_via_http(repo_owner, repo_name, pr.number, github_token)

            if diff_text is None or len(diff_text) < 100:
                raise ValueError("Diff content is missing or too short. Cannot process.")

            if len(diff_text) > MAX_DIFF_CHARS:
                diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... (Diff content truncated)"
                print(f"   -> WARNING: Diff content for PR #{pr.number} was truncated.")

            full_content_for_gemini = f"PR Description: {original_summary_full}\n\nCode Diff:\n{diff_text}"

        except Exception as e:
            print(f"   -> Could not retrieve diff/details for PR #{pr.number}. Skipping. Error: {e}")
            print(f"   -> Waiting {PR_SLEEP_TIME} second(s) before next PR...")
            time.sleep(PR_SLEEP_TIME)
            continue

        time.sleep(PR_SLEEP_TIME)

        summary_data = generate_summary(full_content_for_gemini, gemini_api_key)

        if summary_data:
            results.append({
                "PR #": pr.number,
                "Original PR Title": pr.title,
                "Generated PR Title": summary_data.get("Generated_Title", "N/A"),
                "Original PR Summary": original_summary_full[:500] + (
                    "..." if len(original_summary_full) > 500 else ""),
                "Generated PR Summary": summary_data.get("Generated_Summary", "N/A")
            })
        else:
            print(f"   -> Failed to generate summary for PR #{pr.number}.")

    save_results_to_csv(results, OUTPUT_FILENAME)