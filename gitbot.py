import ConfigParser
import json
import os
from subprocess import call, check_output, CalledProcessError
import shlex
import tempfile
import textwrap

from bs4 import BeautifulSoup

import flask
from flask import request, Response

import requests.api
import requests.auth

# TODO
# Features:
# 1. Call status API to change merge button if there are fixup! or squash!
# commits in the branch
# 2. Call status API to change merge button if there are commits that fail the
# test suite
#   a. Could be done on code repos
#   b. Difficult to do on server repos
# In the link, could provide the commands to run as a url parameter
# ?commands="flake8 --config=flake8.conf && nosetests"
# We can determine which commit failed by looking at the
# $GIT_DIR/rebase-merge/done file.  The last line is the command that
# failed.  The previous line that starts with "pick" is the commit that
# failed.

app = flask.Flask(__name__)

config = ConfigParser.ConfigParser()
with open('test.cfg') as f:
    config.readfp(f)

USERNAME = config.get('github', 'username')
PERSONAL_ACCESS_TOKEN = config.get('github', 'personal_access_token')
GITHUB_API_ENDPOINT = config.get('github', 'endpoint')
GITHUB_HOSTNAME = config.get('github', 'hostname')

def _generate_html_diff(diff_output):
    """Take a diff string and convert it to syntax highligted HTML

    This takes a diff string and runs it through vim's TOhtml script to
    generate HTML that shows a rendered diff with syntax highlighting.

    Args:
        diff_output: The diff as a string

    Returns:
        HTML output that corresponds to a syntax highlighted diff file.
    """
    diff_output_file = tempfile.NamedTemporaryFile(delete=False)
    with open(diff_output_file.name, 'w') as t:
        t.write(diff_output)

    # Use the default colorscheme on a light background for the
    # generated html
    diff_colorize_cmd = shlex.split(
        'vim '
        '-c "set bg=light" '
        '-c "colo default" '
        '-c "let g:html_no_progress=1" '
        '-c "let g:html_number_lines=1" '
        '-c "let g:html_prevent_copy=\\"n\\"" '
        '-c "let g:html_ignore_folding=1" '
        '-c "TOhtml" '
        '-c "wqa" '
        '-- {diff_output_file}'.format(diff_output_file=diff_output_file.name))
    subprocess.call(diff_colorize_cmd)

    with open('{0}.html'.format(diff_output_file.name)) as o:
        html_diff_output = o.read()

    # Remove the temporary files
    os.unlink(diff_output_file.name)
    os.unlink(diff_output_file.name + '.html')

    return html_diff_output


def _generate_side_by_side_html_diff(
        string1_output, string2_output, string3_output=None,
        string4_output=None):
    """Generate HTML rendering of a side-by-side diff

    This takes two to four strings and writes them to temporary files.
    Then it runs vim to open all the files with a vertical split, uses the
    TOhtml vim script to convert that into an html file which is then
    written to another temp file.  The content of that file is read into
    a variable.  Then all temp files are deleted and the string is
    returned to the caller.

    Args:
        string1_output: The string on the left side of the side-by-side
            diff.
        string2_output: The string on the right side of the side-by-side
            diff.
        string3_output: If provided, the third window in a series diff
        string3_output: If provided, the fourth window in a series diff

    Returns:
        A string containing the HTML rendering of the side-by-side diff,
        or if more than two strings are provided, a series side-by-side
        diff.
    """
    string_output_files = []
    for string_output in [
            string1_output, string2_output, string3_output,
            string4_output]:
        if not string_output:
            break
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        with open(temp_file) as t:
            t.write(string_output)
        string_output_files.append(temp_file.name)
    
    side_by_side_html_output_file = tempfile.NamedTemporaryFile(delete=False)
    vim_side_by_side_cmd = shlex.split(
        'vim -O '
        '-c "set bg=light" '
        '-c "colo default" '
        '-c "windo diffthis" '
        '-c "let g:html_number_lines=1" '
        '-c "let g:html_no_progress=1" '
        '-c "let g:html_prevent_copy=\\"n\\"" '
        '-c "let g:html_ignore_folding=1" '
        '-c "TOhtml" '
        '-c "w! {side_by_side_html_file}" '
        '-c "qa!" '
        '-- {string_output_files}'.format(
            side_by_side_html_output_file=side_by_side_html_output_file.name,
            string_output_files=' '.join(string_output_files))
 
    subprocess.call(vim_side_by_side_cmd)
 
    with open(side_by_side_html_output_file.name) as t:
        side_by_side_html_output = t.read()
 
    # Remove temp files
    os.unlink(side_by_side_html_output_file.name)
    for string_output_file in string_output_files:
        os.unlink(string_output_file)

    return side_by_side_html_output


def _generate_github_rebase_comment(url_root, base_branch_name, latest_rebase):
    rebase_diff_url_params = {
        'url_root': url_root,
        'branch_name': base_branch_name,
        'start_rebase_number': latest_rebase,
        'end_rebase_number': latest_rebase + 1,
    }
    rebase_diff_url_template = (
        '{url_root}/rebase_diff?'
        'branch_name={branch_name}&'
        'rebase_start={start_branch_pointer}-{start_rebase_number}&'
        'rebase_end={end_branch_pointer}-{end_rebase_number}&'
        'side_by_side={side_by_side}'
    )
 
    rebase_diff_links = {
        'head_to_head': rebase_diff_url_template.format(
            start_branch_pointer='head', end_branch_pointer='head',
            side_by_side=0, **rebase_diff_url_params),
        'head_to_head_side_by_side': rebase_diff_url_template.format(
            start_branch_pointer='head', end_branch_pointer='head',
            side_by_side=1, **rebase_diff_url_params),
    
        'base_to_base': rebase_diff_url_template.format(
            start_branch_pointer='base', end_branch_pointer='base',
            side_by_side=0, **rebase_diff_url_params),
        'base_to_base_side_by_side': rebase_diff_url_template.format(
            start_branch_pointer='base', end_branch_pointer='base',
            side_by_side=1, **rebase_diff_url_params),
    
        'head_to_base': rebase_diff_url_template.format(
            start_branch_pointer='head', end_branch_pointer='base',
            side_by_side=0, **rebase_diff_url_params),
        'head_to_base_side_by_side': rebase_diff_url_template.format(
            start_branch_pointer='head', end_branch_pointer='base',
            side_by_side=1, **rebase_diff_url_params),
    }
 
    rebase_diff_block = textwrap.dedent('''
        * Rebase diff
          - [head to head]({head_to_head})
            + [side by side]({head_to_head_side_by_side})
          - [base to base]({base_to_base})
            + [side by side]({base_to_base_side_by_side})
          - [head to base]({head_to_base})
            + [side by side]({head_to_base_side_by_side})\
    '''.format(**rebase_diff_links))
 
    rebase_commit_log_url_params = {
        'url_root': url_root,
        'branch_name': base_branch_name,
        'start_rebase_number': latest_rebase,
        'end_rebase_number': latest_rebase + 1,
    }
    rebase_commit_log_url_template = (
        '{url_root}/rebase_commit_log_diff?'
        'branch_name={branch_name}&'
        'rebase_start={start_branch_pointer}-{start_rebase_number}&'
        'rebase_end={end_branch_pointer}-{end_rebase_number}&'
        'side_by_side={side_by_side}&'
        'show_diffs={show_diffs}'
    )
    rebase_commit_log_links = {
        'head_to_head': rebase_commit_log_url_template.format(
            start_branch_pointer='head', end_branch_pointer='head',
            side_by_side=0, show_diffs=0,
            **rebase_commit_log_url_params),
        'head_to_head_show_diffs': rebase_commit_log_url_template.format(
            start_branch_pointer='head', end_branch_pointer='head',
            side_by_side=0, show_diffs=1,
            **rebase_commit_log_url_params),
        'head_to_head_side_by_side': 
            rebase_commit_log_url_template.format(
                start_branch_pointer='head', end_branch_pointer='head',
                side_by_side=1, show_diffs=0,
                **rebase_commit_log_url_params),
        'head_to_head_side_by_side_show_diffs': 
            rebase_commit_log_url_template.format(
                start_branch_pointer='head', end_branch_pointer='head',
                side_by_side=1, show_diffs=1,
                **rebase_commit_log_url_params),
    
        'base_to_base': rebase_commit_log_url_template.format(
            start_branch_pointer='base', end_branch_pointer='base',
            side_by_side=0, show_diffs=0,
            **rebase_commit_log_url_params),
        'base_to_base_show_diffs':
            rebase_commit_log_url_template.format(
                start_branch_pointer='base', end_branch_pointer='base',
                side_by_side=0, show_diffs=1,
                **rebase_commit_log_url_params),
        'base_to_base_side_by_side':
            rebase_commit_log_url_template.format(
                start_branch_pointer='base', end_branch_pointer='base',
                side_by_side=1, show_diffs=0,
                **rebase_commit_log_url_params),
        'base_to_base_side_by_side_show_diffs':
            rebase_commit_log_url_template.format(
                start_branch_pointer='base', end_branch_pointer='base',
                side_by_side=1, show_diffs=1,
                **rebase_commit_log_url_params),
    
        'head_to_base': rebase_commit_log_url_template.format(
            start_branch_pointer='head', end_branch_pointer='base',
            side_by_side=0, show_diffs=0,
            **rebase_commit_log_url_params),
        'head_to_base_show_diffs':
            rebase_commit_log_url_template.format(
                start_branch_pointer='head', end_branch_pointer='base',
                side_by_side=0, show_diffs=1,
                **rebase_commit_log_url_params),
        'head_to_base_side_by_side':
            rebase_commit_log_url_template.format(
                start_branch_pointer='head', end_branch_pointer='base',
                side_by_side=1, show_diffs=0,
                **rebase_commit_log_url_params),
        'head_to_base_side_by_side_show_diffs':
            rebase_commit_log_url_template.format(
                start_branch_pointer='head', end_branch_pointer='base',
                side_by_side=1, show_diffs=1,
                **rebase_commit_log_url_params),
    }
 
    rebase_commit_log_block = textwrap.dedent('''
    * Rebase commit log diff
      - [head to head]({head_to_head})
        + [with diffs]({head_to_head_show_diffs})
        + [side by side]({head_to_head_side_by_side})
        + [side by side with diffs]({head_to_head_side_by_side_show_diffs})
      - [base to base]({base_to_base})
        + [with diffs]({base_to_base_show_diffs})
        + [side by side]({base_to_base_side_by_side})
        + [side by side with diffs]({base_to_base_side_by_side_show_diffs})
      - [head to base]({head_to_base})
        + [with diffs]({head_to_base_show_diffs})
        + [side by side]({head_to_base_side_by_side})
        + [side by side with diffs]({head_to_base_side_by_side_show_diffs})\
    '''.format(**rebase_commit_log_links))
 
    rebase_diff_series_url_template = ''
    rebase_commit_log_series_url_template = ''

    rebase_diff_series_block = ''
    rebase_commit_log_series_block = ''

    rebase_diff_series_url_params = {}
    rebase_commit_log_series_url_params = {}
 
    # If we have at least 3 rebase branches, we can do a series diff
    # (we could do it with two branches, but it wouldn't be any
    # different compared to the rebase_diff and rebase_commit_log_diff
    # side-by-side views)
    if latest_rebase + 1 == 2:
        rebase_diff_series_url_params = {
            'url_root': url_root,
            'branch_name': base_branch_name,
            'first_rebase_number': latest_rebase - 1,
            'second_rebase_number': latest_rebase,
            'third_rebase_number': latest_rebase + 1,
        }
        rebase_diff_series_url_template = (
            '{url_root}/rebase_diff_series?'
            'branch_name={branch_name}&'
            'rebase_first={first_branch_pointer}-{first_rebase_number}&'
            'rebase_second={second_branch_pointer}-{second_rebase_number}&'
            'rebase_third={third_branch_pointer}-{third_rebase_number}'
        )
 
        rebase_commit_log_series_url_params = {
            'url_root': url_root,
            'branch_name': base_branch_name,
            'first_rebase_number': latest_rebase - 1,
            'second_rebase_number': latest_rebase,
            'third_rebase_number': latest_rebase + 1,
        }
        rebase_commit_log_series_url_template = (
            '{url_root}/rebase_commit_log_series?'
            'branch_name={branch_name}&'
            'rebase_first={first_branch_pointer}-{first_rebase_number}&'
            'rebase_second={second_branch_pointer}-{second_rebase_number}&'
            'rebase_third={third_branch_pointer}-{third_rebase_number}&'
            'show_diffs={show_diffs}'
        )
    # We have at least 4 rebase branches, so we can use all rebase_*
    # url parameters
    elif latest_rebase + 1 >= 3:
        rebase_diff_series_url_params = {
            'url_root': url_root,
            'branch_name': base_branch_name,
            'first_rebase_number': latest_rebase - 2,
            'second_rebase_number': latest_rebase -1,
            'third_rebase_number': latest_rebase,
            'fourth_rebase_number': latest_rebase + 1
        }
        rebase_diff_series_url_template = (
            '{url_root}/rebase_diff_series?'
            'branch_name={branch_name}&'
            'rebase_first={first_branch_pointer}-{first_rebase_number}&'
            'rebase_second={second_branch_pointer}-{second_rebase_number}&'
            'rebase_third={third_branch_pointer}-{third_rebase_number}&'
            'rebase_fourth={fourth_branch_pointer}-{fourth_rebase_number}'
        )
 
        rebase_commit_log_series_url_params = {
            'url_root': url_root,
            'branch_name': base_branch_name,
            'first_rebase_number': latest_rebase - 2,
            'second_rebase_number': latest_rebase - 1,
            'third_rebase_number': latest_rebase,
            'fourth_rebase_number': latest_rebase + 1,
        }
        rebase_commit_log_series_url_template = (
            '{url_root}/rebase_commit_log_series?'
            'branch_name={branch_name}&'
            'rebase_first={first_branch_pointer}-{first_rebase_number}&'
            'rebase_second={second_branch_pointer}-{second_rebase_number}&'
            'rebase_third={third_branch_pointer}-{third_rebase_number}&'
            'rebase_fourth={fourth_branch_pointer}-{fourth_rebase_number}&'
            'show_diffs={show_diffs}')
 
    rebase_diff_series_links = {
        'branch_heads': rebase_diff_series_url_template.format(
            first_branch_pointer='head', second_branch_pointer='head',
            third_branch_pointer='head', fourth_branch_pointer='head',
            **rebase_diff_series_url_params),
        'branch_bases': rebase_diff_series_url_template.format(
            first_branch_pointer='head', second_branch_pointer='head',
            third_branch_pointer='head', fourth_branch_pointer='head',
            side_by_side=1, **rebase_diff_series_url_params),
    }
 
    rebase_commit_log_series_links = {
        'branch_heads': rebase_commit_log_series_url_template.format(
            first_branch_pointer='head', second_branch_pointer='head',
            third_branch_pointer='head', fourth_branch_pointer='head',
            show_diffs=0, **rebase_commit_log_series_url_params),
        'branch_heads_show_diffs': 
            rebase_commit_log_series_url_template.format(
                first_branch_pointer='head',
                second_branch_pointer='head',
                third_branch_pointer='head', fourth_branch_pointer='head',
                show_diffs=1, **rebase_commit_log_series_url_params),
        'branch_bases': rebase_commit_log_series_url_template.format(
            first_branch_pointer='base', second_branch_pointer='base',
            third_branch_pointer='base', fourth_branch_pointer='base',
            show_diffs=0, **rebase_commit_log_series_url_params),
        'branch_bases_show_diffs':
            rebase_commit_log_series_url_template.format(
                first_branch_pointer='base',
                second_branch_pointer='base',
                third_branch_pointer='base',
                fourth_branch_pointer='base', show_diffs=1,
                **rebase_commit_log_series_url_params),
    }
 
    if latest_rebase + 1 >= 2:
        rebase_diff_series_block = textwrap.dedent('''
            * Rebase series diff
              - [branch heads]({branch_heads})
              - [branch bases]({branch_bases})
        '''.format(**rebase_diff_series_links))
 
        rebase_commit_log_series_block = textwrap.dedent('''
            * Rebase commit log series diff
              - [branch heads]({branch_heads})
                + [with diffs]({branch_heads_show_diffs})
              - [branch bases]({branch_bases})
                + [with diffs]({branch_bases_show_diffs})
        '''.format(**rebase_commit_log_series_links))
 
    comment_block = textwrap.dedent('''
        {rebase_diff_block}
        {rebase_commit_log_block}
        {rebase_diff_series_block}
        {rebase_commit_log_series_block}
        '''.format(
            rebase_diff_block=rebase_diff_block,
            rebase_commit_log_block=rebase_commit_log_block,
            rebase_diff_series_block=rebase_diff_series_block,
            rebase_commit_log_series_block=rebase_commit_log_series_block))

    return comment_block


def _check_for_fixup_or_squash_commits(log_start_ref, log_end_ref):
    """Check log range for fixup! or squash! commits

    Check whether fixup! or squash! commits are present in the log range.

    Args:
        log_start_ref: ref to exclude commits reachable from it
        log_end_ref: ref to include commits reachable from it

    Returns;
        A list of sha1 values where the shortlog message starts wtih "fixup! "
        or "squash! ".
    """
    # Run git log --oneline 
    # FETCH_HEAD..org/repo/PR/pr_number/base_branch/rebase-head/0
    log_oneline_cmd = shlex.split(
        'git log --format="%H %s"
        {log_start_ref}..{log_end_ref}'.format(
            log_start_ref=log_start_ref, log_end_ref=log_end_ref))
    log_oneline_output = subprocess.check_output(log_oneline_cmd)

    sha1s = []
    for line in log_oneline_output.splitlines():
        if not line:
            continue

        sha1, shortlog = line.split(' ', 1)
        fixup_or_squash = (
            shortlog.startswith('fixup! ') or
            shortlog.startswith('squash! '))

        # We don't care about regular commits here
        if not fixup_or_squash:
            continue
        sha1s.append(sha1)

    return sha1s


# TODO:
# Use the github API to also update the merge status based on whether there
# are fixup or squash commits in the branch.  If there are, we don't want
# people to merge the branch.
#
# POST /repos/:owner/:repo/statuses/:sha
#
# can be used to set the status for a particular commit
#
# This would need to be done when a PR is opened or a commit is pushed to
# the PR branch (which this method already checks for)
#
# Also check if it's possible to check how many approvals a PR has before
# allowing a merge.
#
# GET /repos/:owner/:repo/pulls/:number/reviews
# 
# will return a JSON list of reviews
# check the commit_id value to verify that it matches the latest commit id
# if it does, then check if the state is approved.  We can count the
# approvals this way.
#
# Would need to listen for pull request review events from the webhook to
# update the status
#
# It would also be nice to set the status of a commit based on the linter and
# unit test results.  For server type repos, things get a little harder because
# of the inconsistent environments (kitchen vs vagrant).


@app.route('/check_rebase',methods=['POST'])
def check_rebase():
    url_root = request.url_root

    request_data = json.loads(flask.request.data)
    request_header = flask.request.headers

    event_type = request_header['X-Github-Event']

    # The event is a pull request
    if event_type in ['pull_request']:
        org_repo_name = request_data['repository']['full_name']
        ssh_url = request_data['repository']['ssh_url']
        pr_number = request_data['number']
        action = request_data['action']
        head_branch_name = request_data['pull_request']['head']['ref']
        base_branch_name = request_data['pull_request']['base']['ref']


        # We only want to check for pull requests that have just been
        # opened so that we can retrieve the PR branch and create a
        # local pointer to it.
        if action == 'opened':
            org_name, repo_name = org_repo_name.split('/')

            # Run git fetch to get the objects from the remote
            fetch_cmd = shlex.split(
                'git fetch git@{github_hostname}:{org_repo_name}.git '
                'refs/pull/{pr_number}/head'.format(
                    github_hostname=GITHUB_HOSTNAME,
                    org_repo_name=org_repo_name, pr_number=pr_number))
            subprocess.call(fetch_cmd)

            # We want to create a base and head branch pointers
            # The base branch pointer would be the head of the branch
            # when the branch is created.
            # The head of the branch would advance whenever a commit is
            # added to the branch (not a force-push).  This will allow
            # us to do a diff if fixup or squash commits are added to
            # this branch before its rebased.  Otherwise, we won't be
            # able to get that diff once a rebase takes place.
            # Proposed branch schema:
            # <org-name>/<repo-name>/PR/<PR-number>/<base-branch>/rebase-base/<rebase-number>
            # <org-name>/<repo-name>/PR/<PR-number>/<base-branch>/rebase-head/<rebase-number>
            for branch_pointer in ['base', 'head']:
                branch_cmd = shlex.split(
                    'git branch {org}/{repo}/PR/{pr_number}/'
                    '{base_branch_name}/rebase-{branch_pointer}/0 '
                    'FETCH_HEAD'.format(
                        org=org_name, repo=repo_name, pr_number=pr_number,
                        base_branch_name=base_branch_name,
                        branch_pointer=branch_pointer))

                subprocess.call(branch_cmd)

            # Check list of commits in branch to see if there are any
            # fixup or squash commits
            fetch_base_cmd = shlex.split(
                'git fetch git@{github_hostname}:{org_repo_name}.git '
                'refs/heads/{base_branch_name}')
            subprocess.call(fetch_base_cmd)

            log_start_ref = 'FETCH_HEAD'
            log_end_ref = (
                '{org}/{repo}/PR/{pr_number}/base_branch_name}/'
                'rebase-head/0'.format(
                    org=org_name, repo=repo_name, pr_number=pr_number,
                    base_branch_name=base_branch_name))
            fixup_or_squash_commits = _check_for_fixup_or_squash_commits(
                log_start_ref, log_end_ref) 

            for sha1 in fixup_or_squash_commits:
                # Check the status of the commit
                status_url = (
                    '{endpoint}/repos/{org}/{repo}/commits/{sha1}/'
                    'statuses'.format(
                        endpoint=GITHUB_API_ENDPOINT, org=org_name,
                        repo=repo_name, sha1=sha1))
                http_auth = auth=requests.auth.HTTPBasicAuth(
                    USERNAME, PERSONAL_ACCESS_TOKEN))

                responses_json = requests.api.get(status_url, auth=http_auth)
                responses = responses_json.json()

                status_set = False
                for reponse in responses:
                    if (
                            response['context'] == 'gitbot' and
                            response['state'] == 'failure'):
                        status_set = True
                        break

                # If status is already set, we don't need to set it again
                if status_set:
                    continue

                post_body = {
                    'status': 'failure',
                    'context': 'gitbot',
                    'description': (
                        '"fixup! " or "squash! " commits cannot be merged')
                }

                # Set the status for this commit
                requests.api.post(status_url, json=post_body, auth=http_auth)

    # The event type is a push to the remote
    elif event_type in ['push']:
        sender = request_data['sender']['login']
        sha_before = request_data['before']
        sha_after = request_data['after']
        ssh_url = request_data['repository']['ssh_url']
        org_name, repo_name = request_data['repository']['full_name'].split(
            '/')

        # List remote branches that correspond to pull requests on the remote
        # repo.  Github will create a branch named refs/pull/<PR_NUMBER>/head
        # for each pull request that has been opened on that repo.  We want to
        # find the sha1 value of that branch and compare it to the event sent
        # to use by github (the after value).  The branch that matches, if any,
        # will correspond to the pull request that was just updated.
        git_ls_remote_cmd = shlex.split(
            'git ls-remote {url} refs/pull/*/head'.format(url=ssh_url))
        ls_remote_output = subprocess.check_output(git_ls_remote_cmd)

        target_branch = ''
        for line in ls_remote_output.splitlines():
            if not line:
                continue

            sha1, branch = line.split()
            
            # We're looking for the branch that was just pushed to, so
            # we don't want a branch whose sha1 doesn't match the value
            # of sha_after
            if sha1 != sha_after:
                continue

            target_branch = branch
            break
        
        # If a PR has not been associated with the branch that was just pushed,
        # then return.  This can happen when someone is just pushing up
        # code to Github on a branch that hasn't been pull requested
        # yet.
        if not target_branch:
            return ''

        # Get the PR number for the remote refs/pull/*/head branch
        _, _, pr_number, _ = target_branch.split('/')

        # Branch name format
        # AA/shark-github/PR/4/master/rebase-{base,head}/9
        # Find the branches that correspond to the PR we're working
        # with.  We use rebase-head here since we can find the latest
        # rebase number if the branch was amended or rebased.  If not,
        # then we can just update the rebase-head branch on the current
        # rebase number
        branch_cmd = shlex.split(
            'git branch '
            '--list {org}/{repo}/PR/{pr_number}/*/rebase-head/*'.format(
                org=org_name, repo=repo_name, pr_number=pr_number))

        branch_output = subprocess.check_output(branch_cmd)

        # Find the branch that corresponds to the latest rebase
        latest_rebase = -1
        for line in branch_output.splitlines():
            if not line:
                continue

            branch_name, rebase_number = line.rsplit('/', 1)
            rebase_number = int(rebase_number)
            if rebase_number > latest_rebase:
                latest_rebase = rebase_number

        branch_name = branch_name.strip()

        # Fetch the PR branch into FETCH_HEAD.  This will allow for
        # creating a local branch that points to the head of that branch
        fetch_cmd = shlex.split('git fetch {url} {target_branch}'.format(
            url=ssh_url, target_branch=target_branch))
        subprocess.call(fetch_cmd)

        # Check to see whether this push was a force push.  If it is,
        # then this is a rebase or amended commit.  We do this by checking
        # whether the before sha1 value in the github event is an ancestor of
        # the after sha1 value.  For a regular push, this will be the case.
        # For a force push, this may not be the case.
        merge_base_cmd = shlex.split(
            'git merge-base --is-ancestor {before} {after}'.format(
                before=sha_before, after=sha_after))
        ref_name = request_data['ref']

        is_rebase = subprocess.call(merge_base_cmd)

        # Create new rebase branches off of FETCH_HEAD (for both
        # rebase-base and rebase-head)
        local_branch_name = '{branch_name}/{rebase_number}'
        if is_rebase:
            local_branch_name = local_branch_name.format(
                branch_name=branch_name, rebase_number=latest_rebase + 1)
            # Remove rebase-head from the end of the branch name (rebase
            # number was removed earlier)
            base_branch_name, _ = branch_name.rsplit('/', 1)
            for branch_pointer in ['base', 'head']:
                new_branch_cmd = shlex.split(
                    'git branch {base_branch_name}/rebase-{branch_pointer}/'
                    '{rebase_number} FETCH_HEAD'.format(
                        base_branch_name=base_branch_name,
                        branch_pointer=branch_pointer,
                        rebase_number=latest_rebase + 1))
                subprocess.call(new_branch_cmd)

            comment = _generate_github_rebase_comment(
                url_root, base_branch_name, latest_rebase):


            # Post the comment on the Github PR
            post_url = (
                '{endpoint}/repos/{org}/{repo}/issues/{pr_number}/'
                'comments'.format(
                    endpoint=GITHUB_API_ENDPOINT, org=org_name, repo=repo_name,
                    pr_number=pr_number))
            post_body = {'body': comment},
            http_auth = auth=requests.auth.HTTPBasicAuth(
                USERNAME, PERSONAL_ACCESS_TOKEN))

            response = requests.api.post(
                post_url, json=post_body, auth=http_auth)

        else: # This is not a rebase/amend
            # branch_name ends in rebase-head.  We want to update this
            # branch to point to the new commit. rebase-base will remain
            # at the same commit the head of this branch was after it
            # was last rebased.

            # We could consider checking whether the commits pushed are
            # fixup or squash commits and then posting a comment in the
            # PR that shows who pushed the commits and list their
            # comment message bodies and titles in the comment.
            local_branch_name = local_branch_name.format(
                branch_name=branch_name, rebase_number=latest_rebase)
            update_branch_cmd = shlex.split(
                'git update-ref refs/heads/{branch_name}/{rebase_number} '
                'FETCH_HEAD'.format(
                    branch_name=branch_name, rebase_number=latest_rebase))
            subprocess.call(update_branch_cmd)

        # Determine the base branch of the PR
        # AA/shark-github/PR/4/master/rebase-{base,head}/9
        _, _, _, _, base_branch_name, _, _ = local_branch_name.split('/')

        # Check list of commits in branch to see if there are any
        # fixup or squash commits
        fetch_base_cmd = shlex.split(
            'git fetch git@{github_hostname}:{org_repo_name}.git '
            'refs/heads/{base_branch_name}')
        subprocess.call(fetch_base_cmd)

        log_start_ref = 'FETCH_HEAD'
        log_end_ref = local_branch_name
        fixup_or_squash_commits = _check_for_fixup_or_squash_commits(
            log_start_ref, log_end_ref) 

        for sha1 in fixup_or_squash_commits:
            # Check the status of the commit
            status_url = (
                '{endpoint}/repos/{org}/{repo}/commits/{sha1}/'
                'statuses'.format(
                    endpoint=GITHUB_API_ENDPOINT, org=org_name,
                    repo=repo_name, sha1=sha1))
            http_auth = auth=requests.auth.HTTPBasicAuth(
                USERNAME, PERSONAL_ACCESS_TOKEN))

            responses_json = requests.api.get(status_url, auth=http_auth)
            responses = responses_json.json()

            status_set = False
            for reponse in responses:
                if (
                        response['context'] == 'gitbot' and
                        response['state'] == 'failure'):
                    status_set = True
                    break

            # If status is already set, then we don't need to set it again
            if status_set:
                continue

            post_body = {
                'status': 'failure',
                'context': 'gitbot',
                'description': (
                    '"fixup! " or "squash! " commits cannot be merged')
            }

            # Set the status for this commit
            requests.api.post(status_url, json=post_body, auth=http_auth)

    return ''


@app.route('/rebase_diff',methods=['GET'])
def show_rebase_diff():
    branch_name = request.args.get('branch_name')
    rebase_start = request.args.get('rebase_start')
    rebase_end = request.args.get('rebase_end')
    side_by_side = request.args.get('side_by_side')

    side_by_side = side_by_side == '1'

    org, repo, _, _, base_branch = branch_name.split('/')
    start_branch, start_number = rebase_start.split('-')
    end_branch, end_number = rebase_end.split('-')

    if side_by_side:
        # fetch the base branch from the remote so we can have a local
        # copy of the objects and its sha1 stored in FETCH_HEAD
        git_fetch_base_branch_cmd = shlex.split(
            #'git fetch git@{github_hostname}:{org}/{repo}.git '
            'git fetch ../git-rebase.git '
            'refs/heads/{base_branch}'.format(
                github_hostname=GITHUB_HOSTNAME, org=org, repo=repo,
                base_branch=base_branch))
        subprocess.call(git_fetch_base_branch_cmd)

        git_diff_rebase_start_cmd = shlex.split(
            'git diff '
            'FETCH_HEAD..'
            'refs/heads/{branch_name}/'
            'rebase-{start_branch}/{start_number}'.format(
                branch_name=branch_name, start_branch=start_branch,
                start_number=start_number))
        git_diff_rebase_start_output = subprocess.check_output(
            git_diff_rebase_start_cmd)

        git_diff_rebase_end_cmd = shlex.split(
            'git diff '
            'FETCH_HEAD..'
            'refs/heads/{branch_name}/'
            'rebase-{end_branch}/{end_number}'.format(
                branch_name=branch_name, end_branch=end_branch,
                end_number=end_number))
        git_diff_rebase_end_output = subprocess.check_output(
            git_diff_rebase_end_cmd)

        side_by_side_html_output = _generate_side_by_side_html_diff(
            git_diff_rebase_start_output, git_diff_rebase_end_output)

        # Update the table header titles for each side to match the
        # branch name diffs
        titles = [
            'git diff '
            'refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/rebase-{start_branch}/'
            '{start_number}'.format(
                base_branch=base_branch, branch_name=branch_name,
                start_branch=start_branch, start_number=start_number),
            'git diff '
            'refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/rebase-{end_branch}/'
            '{end_number}'.format(
                base_branch=base_branch, branch_name=branch_name,
                end_branch=end_branch, end_number=end_number),
        ]
        html_parser = BeautifulSoup(side_by_side_html_output, 'html.parser')
        for title, th_element in zip(titles, html_parser.find_all('th')):
            th_element.string = title

        # Update the page title to say Rebase Diff
        html_parser.title.string = 'Rebase Diff'

        return_str = str(html_parser)
    else:
        git_diff_cmd = shlex.split(
            'git diff '
            '--src-prefix='
            '"refs/heads/{branch_name}/rebase-{start_branch}/{start_number}:" '
            '--dst-prefix='
            '"refs/heads/{branch_name}/rebase-{end_branch}/{end_number}:" '
            'refs/heads/{branch_name}/rebase-{start_branch}/{start_number}..'
            'refs/heads/{branch_name}/rebase-{end_branch}/{end_number}'.format(
                branch_name=branch_name, start_branch=start_branch,
                start_number=start_number, end_branch=end_branch,
                end_number=end_number))

        git_diff_output = subprocess.check_output(git_diff_cmd)

        return_str = ''

        # There was no diff, then just return a message stating that
        if not git_diff_output:
            return_str = (
                '<html>'
                '<title>Rebase Diff</title>'
                '<body>No code changed in rebase</body>'
                '</html>')
            return Response(response=return_str, status=200)

        html_diff_output = _generate_html_diff(git_diff_output)

        # Set the title to Rebase Diff
        html_parser = BeautifulSoup(html_diff_output, 'html.parser')
        html_parser.title.string = 'Rebase Diff'

    return Response(response=return_str, status=200)


@app.route('/rebase_commit_log_diff', methods=['GET'])
def show_rebase_commit_log_diff():
    branch_name = request.args.get('branch_name')
    rebase_start = request.args.get('rebase_start')
    rebase_end = request.args.get('rebase_end')
    show_diffs = request.args.get('show_diffs')
    side_by_side = request.args.get('side_by_side')

    show_diffs = show_diffs == '1'
    side_by_side = side_by_side == '1'

    start_branch, start_number = rebase_start.split('-')
    end_branch, end_number = rebase_end.split('-')

    # AA/shark-github/PR/6/master
    org, repo, _, _, base_branch = branch_name.split('/')

    # fetch the base branch from the remote so we can have a local copy of
    # the objects and its sha1 stored in FETCH_HEAD
    git_fetch_base_branch_cmd = shlex.split(
        'git fetch git@{github_hostname}:{org}/{repo}.git '
        'refs/heads/{base_branch}'.format(
            github_hostname=GITHUB_HOSTNAME, org=org, repo=repo,
            base_branch=base_branch))
    subprocess.call(git_fetch_base_branch_cmd)

    rebase_start_cmd = shlex.split(
        'git log {patch} '
        'FETCH_HEAD..'
        'refs/heads/{branch_name}/rebase-{start_branch}/{start_number}'.format(
            patch='-p' if show_diffs else '', branch_name=branch_name,
            start_branch=start_branch, start_number=start_number))
    rebase_start_output = subprocess.check_output(rebase_start_cmd)

    rebase_end_cmd = shlex.split(
        'git log {patch} '
        'FETCH_HEAD..'
        'refs/heads/{branch_name}/rebase-{end_branch}/{end_number}'.format(
            patch='-p' if show_diffs else '', branch_name=branch_name,
            end_branch=end_branch, end_number=end_number))
    rebase_end_output = subprocess.check_output(rebase_end_cmd)

    if side_by_side:
        side_by_side_html_output = _generate_side_by_side_html_diff(
            rebase_start_output, rebase_end_output)

        # Update the table header titles for each side to match the
        # branch name diffs
        titles = [
            'git log {patch} '
            'refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/rebase-{start_branch}/'
            '{start_number}'.format(
                patch='-p' if show_diffs else '',
                base_branch=base_branch, branch_name=branch_name,
                start_branch=start_branch, start_number=start_number),
            'git log {patch} '
            'refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/rebase-{end_branch}/'
            '{end_number}'.format(
                patch='-p' if show_diffs else '',
                base_branch=base_branch, branch_name=branch_name,
                end_branch=end_branch, end_number=end_number),
        ]
        html_parser = BeautifulSoup(side_by_side_html_output, 'html.parser')
        for title, th_element in zip(titles, html_parser.find_all('th')):
            th_element.string = title

        # Update the page title to say Commit Log Diff
        html_parser.title.string = 'Commit Log Diff'

        return_str = str(html_parser)
    else:
        # Create temporary files for the diff command
        rebase_start_temp_file = tempfile.NamedTemporaryFile(delete=False)
        with open(rebase_start_temp_file.name, 'w') as s:
            s.write(rebase_start_output)

        rebase_end_temp_file = tempfile.NamedTemporaryFile(delete=False)
        with open(rebase_end_temp_file.name, 'w') as e:
            e.write(rebase_end_output)

        rebase_diff_cmd = shlex.split(
            'diff '
            '-u '
            '--label='
            '"git log {patch} refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/{start_branch}/{start_number}" '
            '--label='
            '"git log {patch} refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/{end_branch}/{end_number}" '
            '{rebase_start_file} {rebase_end_file}'.format(
                patch='-p' if show_diffs else '',
                base_branch=base_branch, branch_name=branch_name,
                start_branch=start_branch, start_number=start_number,
                end_branch=end_branch, end_number=end_number,
                rebase_start_file=rebase_start_temp_file.name,
                rebase_end_file=rebase_end_temp_file.name))

        # diff exits with a non-zero code if there is a difference between
        # the files.
        try:
            rebase_log_diff_output = subprocess.check_output(rebase_diff_cmd)
        except subprocess.CalledProcessError as e:
            rebase_log_diff_output = e.output

        # Remove the temporary files
        os.unlink(rebase_start_temp_file.name)
        os.unlink(rebase_end_temp_file.name)

        return_str = ''
        if not rebase_log_diff_output:
            return_str = (
                '<html><title>Commit Log Diff</title>'
                '<body>Commit logs have not changed</body>'
                '</html>')
            return Response(response=return_str, status=200)

        html_diff_output = _generate_html_diff(rebase_log_diff_output)

        # Set the title to Rebase Commit Log Diff
        html_parser = BeautifulSoup(html_diff_output, 'html.parser')
        html_parser.title.string = 'Rebase Commit Log Diff'

        return_str = str(html_parser)
    return Response(response=return_str, status=200)


@app.route('/rebase_diff_series', methods=['GET'])
def show_rebase_diff_series():
    branch_name = request.args.get('branch_name')
    rebase_first = request.args.get('rebase_first')
    rebase_second = request.args.get('rebase_second')
    rebase_third = request.args.get('rebase_third')
    rebase_fourth = request.args.get('rebase_fourth')

    org, repo, _, _, base_branch = branch_name.split('/')

    # Loop through the rebase branches until we get to an undefined
    # value
    rebase_branches = []
    for rebase_branch in [
            rebase_first, rebase_second, rebase_third,
            rebase_fourth]:
        if not rebase_branch:
            break
        rebase_branches.append(rebase_branch.split('-'))

    if len(rebase_branches) < 2:
        return_str = (
            '<html><title>Series Diff</title><body>You must have at least two '
            'branches to show a series diff</body></html>')
        return Response(response=return_str, status=200)

    # fetch the base branch from the remote so we can have a local
    # copy of the objects and its sha1 stored in FETCH_HEAD
    git_fetch_base_branch_cmd = shlex.split(
        #'git fetch git@{github_hostname}:{org}/{repo}.git '
        'git fetch ../git-rebase.git '
        'refs/heads/{base_branch}'.format(
            github_hostname=GITHUB_HOSTNAME, org=org, repo=repo,
            base_branch=base_branch))
    subprocess.call(git_fetch_base_branch_cmd)

    # Generate the rebase commands
    git_diff_rebase_cmds = []
    for rebase_branch in rebase_branches:
        git_diff_rebase_cmd = shlex.split(
            'git diff '
            'FETCH_HEAD..'
            'refs/heads/{branch_name}/rebase-{branch}/{number}'.format(
                branch_name=branch_name, branch=rebase_branch[0],
                number=rebase_branch[1]))
        git_diff_rebase_cmds.append(git_diff_rebase_cmd)

    # Get the rebase diff outputs
    git_diff_rebase_outputs = []
    for git_diff_rebase_cmd in git_diff_rebase_cmds:
        git_diff_rebase_output = subprocess.check_output(git_diff_rebase_cmd)
        git_diff_rebase_outputs.append(git_diff_rebase_output)

    series_html_output = _generate_side_by_side_html_diff(
        *git_diff_rebase_outputs)

    titles = []
    for rebase_branch in rebase_branches:
        title = (
            'git diff '
            'refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/rebase-{branch}/{number}'.format(
                base_branch=base_branch, branch_name=branch_name,
                branch=rebase_branch[0], number=rebase_branch[1]))
        titles.append(title)
    
    html_parser = BeautifulSoup(series_html_output, 'html.parser')
    for title, th_element in zip(titles, html_parser.find_all('th')):
        th_element.string = title

    # Update the page title to say Rebase Series Diff
    html_parser.title.string = 'Rebase Series Diff'

    return_str = str(html_parser)

    return Response(response=return_str, status=200)


@app.route('/rebase_commit_log_series', methods=['GET'])
def show_rebase_commit_log_series():
    branch_name = request.args.get('branch_name')
    rebase_first = request.args.get('rebase_first')
    rebase_second = request.args.get('rebase_second')
    rebase_third = request.args.get('rebase_third')
    rebase_fourth = request.args.get('rebase_fourth')
    show_diffs = request.args.get('show_diffs')

    show_diffs = show_diffs == '1'

    org, repo, _, _, base_branch = branch_name.split('/')

    # Loop through the rebase branches until we get to an undefined
    # value
    rebase_branches = []
    for rebase_branch in [
            rebase_first, rebase_second, rebase_third,
            rebase_fourth]:
        if not rebase_branch:
            break
        rebase_branches.append(rebase_branch.split('-'))

    if len(rebase_branches) < 2:
        return_str = (
            '<html><title>Series Log</title><body>You must have at least two '
            'branches to show a series log</body></html>')
        return Response(response=return_str, status=200)

    # fetch the base branch from the remote so we can have a local
    # copy of the objects and its sha1 stored in FETCH_HEAD
    git_fetch_base_branch_cmd = shlex.split(
        #'git fetch git@{github_hostname}:{org}/{repo}.git '
        'git fetch ../git-rebase.git '
        'refs/heads/{base_branch}'.format(
            github_hostname=GITHUB_HOSTNAME, org=org, repo=repo,
            base_branch=base_branch))
    subprocess.call(git_fetch_base_branch_cmd)

    # Generate the rebase commands
    git_log_rebase_cmds = []
    for rebase_branch in rebase_branches:
        git_log_rebase_cmd = shlex.split(
            'git log {patch} '
            'FETCH_HEAD..'
            'refs/heads/{branch_name}/rebase-{branch}/{number}'.format(
                patch='-p' if show_diffs else '',
                branch_name=branch_name, branch=rebase_branch[0],
                number=rebase_branch[1]))
        git_log_rebase_cmds.append(git_log_rebase_cmd)

    # Get the rebase log outputs
    git_log_rebase_outputs = []
    for git_log_rebase_cmd in git_log_rebase_cmds:
        git_log_rebase_output = subprocess.check_output(git_log_rebase_cmd)
        git_log_rebase_outputs.append(git_log_rebase_output)

    series_html_output = _generate_side_by_side_html_diff(
        *git_log_rebase_outputs)

    titles = []
    for rebase_branch in rebase_branches:
        title = (
            'git log {patch} '
            'refs/heads/{base_branch}..'
            'refs/heads/{branch_name}/rebase-{branch}/{number}'.format(
                patch = '-p' if show_diffs else '',
                base_branch=base_branch, branch_name=branch_name,
                branch=rebase_branch[0], number=rebase_branch[1]))
        titles.append(title)
    
    html_parser = BeautifulSoup(series_html_output, 'html.parser')
    for title, th_element in zip(titles, html_parser.find_all('th')):
        th_element.string = title

    # Update the page title to say Rebase Series Diff
    html_parser.title.string = 'Rebase Log Series Diff'

    return_str = str(html_parser)

    return Response(response=return_str, status=200)


if __name__ == '__main__':
    app.run('10.4.20.87', 8000)
