import ConfigParser
import json
import os
import re
import shlex
import subprocess
import tempfile
import textwrap
import time

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
GITHUB_VALID_DOMAINS = config.get('github', 'domains')

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
        with open(temp_file.name, 'w') as t:
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
        '-c "w! {side_by_side_html_output_file}" '
        '-c "qa!" '
        '-- {string_output_files}'.format(
            side_by_side_html_output_file=side_by_side_html_output_file.name,
            string_output_files=' '.join(string_output_files)))
 
    subprocess.call(vim_side_by_side_cmd)
 
    with open(side_by_side_html_output_file.name) as t:
        side_by_side_html_output = t.read()
 
    # Remove temp files
    os.unlink(side_by_side_html_output_file.name)
    for string_output_file in string_output_files:
        os.unlink(string_output_file)

    return side_by_side_html_output


def _generate_github_rebase_comment(
        sender, url_root, base_branch_name, latest_rebase):
    '''Generate text and links for comment posted after rebase

    This will generate text for a comment to be posted once a force push
    is made to a branch that's currently associated with a pull request.

    It will post links showing the change to the overall diff of the
    branch compared to the base branch, the diff of the commit logs
    (along with their associated diffs), and a series diff of the most
    recent (up to) 4 revisions of the branch and the associated commit
    log.

    For BASE to BASE branch diffs, any changes made in branch before
    rebasing will show up in the diff.  For HEAD to BASE diffs, only
    changes introduced during the rebase process will show up in the
    diff.  This may occur if another branch was merged into the base
    branch of the pull request, or if the person accidentally or
    intentionally introduced a change while rebasing.

    For BASE to BASE commit log diffs, any changes in the set of the
    commit messages or the commit messages themselves will show up in
    the diff.  If the show diffs option is enabled, this will also show
    any changes in the diff associated with the commit.  For HEAD to
    BASE commit log diffs, the same types of changes will show up in the
    diff though these changes will show the removal of the fixup! and/or
    squash! in the diff.

    For the BASE branch diff series, the changes introduced after each
    rebase (up to 4) will show up in the side-by-side diff.  For the
    HEAD branch diff series, the diff will show those changes made
    before each rebase.

    For the BASE commit log series, the changes in the commit messages
    (content and ordering) will show up.  If the show diffs options is
    enabled, this will also show any changes in the diff associated with
    the commit.  For the HEAD commit log series, the same types of
    changes will show up in the diff, but you will be able to see the
    fixup! or squash! commits made before each rebase.

    Args:
        sender: The github username of the person who force pushed to
            the PR branch
        url_root: The hostname of the server to prepend to complete the
            urls needed to render the diffs
        base_branch_name: The name of the branch that corresponds to the
            pull request (excluding the rebase count)
        latest_rebase: The number of the current rebase.  This is used
            to determine which branches to check when rending diffs

    Returns:
        A string that will be posted to Github as a comment (which
        contains information and links to the various diffs associated
        with a rebase.
    '''
    rebase_diff_url_params = {
        'url_root': url_root,
        'branch_name': base_branch_name,
        'start_rebase_number': latest_rebase,
        'end_rebase_number': latest_rebase + 1,
    }
    rebase_diff_url_template = (
        '{url_root}rebase_diff?'
        'branch_name={branch_name}&'
        'rebase_start={start_branch_pointer}-{start_rebase_number}&'
        'rebase_end={end_branch_pointer}-{end_rebase_number}&'
        'side_by_side={side_by_side}'
    )
 
    rebase_diff_links = {
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
 
    rebase_diff_base_to_base_links = (
        '[base to base]({base_to_base}) '
        '([side by side]({base_to_base_side_by_side}))'.format(
            **rebase_diff_links))
    rebase_diff_head_to_base_links = (
        '[head to base]({head_to_base}) '
        '([side by side]({head_to_base_side_by_side}))'.format(
            **rebase_diff_links))
    rebase_diff_block = textwrap.dedent('''
        * Rebase diff {base_to_base_links} {head_to_base_links}\
    '''.format(
        base_to_base_links=rebase_diff_base_to_base_links,
        head_to_base_links=rebase_diff_head_to_base_links))

    rebase_commit_log_url_params = {
        'url_root': url_root,
        'branch_name': base_branch_name,
        'start_rebase_number': latest_rebase,
        'end_rebase_number': latest_rebase + 1,
    }
    rebase_commit_log_url_template = (
        '{url_root}rebase_commit_log_diff?'
        'branch_name={branch_name}&'
        'rebase_start={start_branch_pointer}-{start_rebase_number}&'
        'rebase_end={end_branch_pointer}-{end_rebase_number}&'
        'side_by_side={side_by_side}&'
        'show_diffs={show_diffs}'
    )
    rebase_commit_log_links = {
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
 
    rebase_commit_log_base_to_base_links = (
        '[base to base]({base_to_base}) '
        '([with diffs]({base_to_base_show_diffs})) '
        '([side by side]({base_to_base_side_by_side})) '
        '([side by side with diffs]'
        '({base_to_base_side_by_side_show_diffs}))'.format(
            **rebase_commit_log_links))

    rebase_commit_log_head_to_base_links = (
        '[head to base]({head_to_base}) '
        '([with diffs]({head_to_base_show_diffs})) '
        '([side by side]({head_to_base_side_by_side})) '
        '([side by side with diffs]'
        '({head_to_base_side_by_side_show_diffs}))'.format(
            **rebase_commit_log_links))


    rebase_commit_log_block = textwrap.dedent('''
    * Rebase commit log diff
      - {base_to_base_links}
      - {head_to_base_links}\
    '''.format(
        base_to_base_links=rebase_commit_log_base_to_base_links,
        head_to_base_links=rebase_commit_log_head_to_base_links))
 
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
            '{url_root}rebase_diff_series?'
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
            '{url_root}rebase_commit_log_series?'
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
            '{url_root}rebase_diff_series?'
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
            '{url_root}rebase_commit_log_series?'
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
            first_branch_pointer='base', second_branch_pointer='base',
            third_branch_pointer='base', fourth_branch_pointer='base',
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
            * Rebase series diff [branch heads]({branch_heads}) \
            [branch bases]({branch_bases})'''.format(
                **rebase_diff_series_links))
 
        rebase_commit_log_series_branch_head_links = (
            '[branch heads]({branch_heads}) '
            '([with diffs]({branch_heads_show_diffs}))'.format(
                **rebase_commit_log_series_links))
        rebase_commit_log_series_branch_base_links = (
            '[branch bases]({branch_bases}) '
            '([with diffs]({branch_bases_show_diffs}))'.format(
                **rebase_commit_log_series_links))
        rebase_commit_log_series_block = textwrap.dedent('''
            * Rebase commit log series diff {branch_head_links} \
            {branch_base_links}'''.format(
                branch_head_links=rebase_commit_log_series_branch_head_links,
                branch_base_links=rebase_commit_log_series_branch_base_links))

    sender_block = textwrap.dedent('''
        Branch rebased {latest_rebase} time(s), most recently by {sender}\
    '''.format(sender=sender, latest_rebase=latest_rebase+1))
 
    comment_block = textwrap.dedent('''
        {sender_block}\
        {rebase_diff_block}\
        {rebase_commit_log_block}\
        {rebase_diff_series_block}\
        {rebase_commit_log_series_block}
        '''.format(
            sender_block=sender_block,
            rebase_diff_block=rebase_diff_block,
            rebase_commit_log_block=rebase_commit_log_block,
            rebase_diff_series_block=rebase_diff_series_block,
            rebase_commit_log_series_block=rebase_commit_log_series_block))

    return comment_block


def _validate_email(email_addr, addr_type):
    """Check the Committer and Author values of a commit

    Check whether the Committer and Author values are using their first
    and last name as the display name and a valid domain in their email
    address.

    If the values do not pass the checks, then return a list of strings
    that explain each error.

    Args:
        email_addr: Address corresponding to the commit
        addr_type: either 'Author' or 'Committer'

    Returns:
        A list of strings corresponding to each error
    """
    errors = []
    display_name, email_address = email_addr.rsplit(' ', 1)
    email_address = email_address.strip('<>')

    if display_name == 'root':
        errors.append((
            '{addr_type}-root-check'.format(addr_type=lower(addr_type)),
            '{addr_type} is root instead of real name'.format(
                addr_type=addr_type)))

    if ' ' not in display_name:
        errors.append((
            '{addr_type}-real-name-check'.format(addr_type=lower(addr_type)),
            '{addr_type} does not contain first and last name'.format(
                addr_type=addr_type)))

    email_local, email_domain = email_address.rsplit('@', 1)

    github_domain_list = filter(lambda x: x, GITHUB_VALID_DOMAINS.splitlines())
    if email_domain not in github_domain_list:
        errors.append((
            '{addr_type}-valid-domain-check'.format(addr_type=lower(addr_type)),
            '{addr_type} email address domain must be in {domains}'.format(
                addr_type=addr_type, domains=github_domain_list)))

    return errors


def _validate_commit(
        commit_sha1, merge, author, committer, title, separator, body):
    """Check the commit message and commit diff.

    The commit message is checked for the following

    * Title is 50 characters or less
    * Title is in imperative mood
    * Title begins with a capital letter
    * Title beings with verb (see commit_title_start_words)
    * Title does not end in punctuation or whitespace
    * There is a blank line separating the title and body
    * The commit message body lines do not exceed 72 characters
    * The commit title doesn't start with fixup! or squash!

    The commit diff is checked to verify that it doesn't introduce
    trailing whitespace or extra blank lines at the end of the file.

    Args:
        commit_sha1: The full sha1 of the commit
        author: The author value of the commit
        merge: Set if the commit is a merge commit
        committer: The committer value of the commit
        separator: The line separating the title and the body of the
            commit message
        body: The body of the commit message (a list of strings where
            each element corresponds to a line in the body)

    Returns:
        A dict where each value is a tuple of the context string and
        corresponding description string (which can be used when
        constructing the post body for the Github commit status
        endpoint)
    """
    errors = []

    # List of words a commit title can start with
    commit_title_start_words = [
        'Add',
        'Bump',
        'Change',
        'Create',
        'Disable',
        'Enable',
        'Fix',
        'Move',
        'Refactor',
        'Remove',
        'Replace',
        'Revert',
        'Set',
        'Update',
        'Upgrade',
        'Use',
    ]
    author_errors = _validate_email(author, 'Author')
    committer_errors = _validate_email(committer, 'Committer')

    if author_errors:
        errors.extend(author_errors)
    if committer_errors:
        errors.extend(committer_errors)

    title_words = title.split(' ', 1)

    # Check if in imperative tense
    if re.search(r'(ed|ing|s)$', title_words[0]):
        errors.append((
            'title-imperative-tense-check',
            'Commit title is not in imperative tense'))

    # Check if first word is capitalized
    if re.match(r'^[^A-Z]', title_words[0]):
        errors.append((
            'title-capitalization-check',
            'Commit title is not capitalized'))

    # Check if title begins with known start word
    if title_words[0] not in commit_title_start_words:
        errors.append((
            'title-verb-check',
            'Commit title does not begin with a verb'))

    # Check if this is a fixup! commit
    if re.match(r'^fixup!', title_words[0]):
        errors.append((
            'title-fixup-check',
            'Commit title starts with fixup! '))

    # Check if this is a squash! commit
    if re.match(r'^squash!', title_words[0]):
        errors.append((
            'title-squash-check',
            'Commit title starts with squash! '))

    # Check if the commit title ends in whitespace or punctuation
    if len(title_words) > 1 and re.search(r'[\s\W]$', title_words[1]):
        errors.append((
            'title-whitespace-punctuation-check',
            'Commit title ends in whitespace or punctuation'))

    # Check if the title is greater than 50 characters in length
    if len(title) > 50:
        errors.append((
            'title-length-check',
            'Commit title longer than 50 characters'))

    # Check if separator line (between title and body) is empty
    if separator is not None and separator != '':
        errors.append((
            'message-separator-check',
            'Missing blank line between title and body'))

    # Check if the commit message has a body
    if body == []:
        errors.append((
            'body-check',
            'Missing commit message body'))

    # Check if any line in the body is greater than 72 characters in legnth
    for body_line in body:
        if len(body_line) <= 72:
            continue
        errors.append((
            'body-length-check',
            'Commit message body line > 72 characters'))
        break

    # Check if commit is a merge commit
    if merge is not None:
        errors.append((
            'commit-merge-check',
            'Commit is a merge commit'))

    # Check commit diff for whitespace errors
    git_diff_cmd = shlex.split(
        'git show --check {commit_sha1}'.format(
            commit_sha1=commit_sha1))

    has_whitespace_issue = None
    f, _ = tempfile.mkstemp()
    has_whitespace_issue = subprocess.call(git_diff_cmd,
        stdout=f, stderr=f, close_fds=True)
    os.close(f)

    if has_whitespace_issue:
        errors.append((
            'diff-whitespace-check',
            'Commit diff has whitespace issues'))

    return errors


def _parse_commit_log(base_commit, tip_commit):
    """Parse the output of git log --format=full <commit_range>

    This parses the output of git log --format-full <commit_range>,
    extracts the commit sha1, author, committer, commit title, commit
    separator, and commit message body values and passes them to other
    methods to validate their format

    Args:
        base_commit: commit sha1 value that it, along with its ancestors
            should be excluded from the git log output
        tip_commit: commit sha1 value that it, along with its ancestors
            should be included in the git log output

    Returns:
        A dict indexed by commit sha1 values where each value is a list of
        strings that describe any issues found for that commit
    """

    class LogState(object):
        SEPARATOR_LINE = 0
        COMMIT_SHA1_LINE = 1
        MERGE_LINE = 2
        AUTHOR_LINE = 3
        COMMITTER_LINE = 4
        MIDDLE_SEPARATOR_LINE = 5
        TITLE_LINE = 6
        BLANK_LINE = 7
        BODY_LINES = 8

    commit_info = {}

    git_log_cmd = shlex.split(
        'git log --format=full {base_commit}..{tip_commit}'.format(
            base_commit=base_commit, tip_commit=tip_commit))
    git_log_output = subprocess.check_output(git_log_cmd)

    log_line_state = LogState.SEPARATOR_LINE
    commit_sha1 = None
    merge = None
    author = None
    committer = None
    title = None
    separator = None
    body = []
    git_log_output_lines = git_log_output.splitlines()
    for idx, line in enumerate(git_log_output_lines, 1):
        # commit line
        if (
                log_line_state == LogState.SEPARATOR_LINE and
                line.startswith('commit ')):
            commit_sha1 = line.split(' ')[1]
            log_line_state = LogState.COMMIT_SHA1_LINE
            continue

        # Merge: line
        if (
                log_line_state == LogState.COMMIT_SHA1_LINE and
                line.startswith('Merge: ')):
            merge = line.split(' ', 1)[1]
            log_line_state = LogState.MERGE_LINE
            continue

        # Author: line
        if (
                log_line_state in [
                    LogState.COMMIT_SHA1_LINE, LogState.MERGE_LINE] and
                line.startswith('Author: ')):
            author = line.split(' ', 1)[1]
            log_line_state = LogState.AUTHOR_LINE
            continue

        # Commit: line
        if log_line_state == LogState.AUTHOR_LINE and line.startswith('Commit: '):
            committer = line.split(' ', 1)[1]
            log_line_state = LogState.COMMITTER_LINE
            continue

        # empty line after Commit: line
        if log_line_state == LogState.COMMITTER_LINE and line == '':
            log_line_state = LogState.MIDDLE_SEPARATOR_LINE
            continue

        # Title line of commit message
        if (
                log_line_state == LogState.MIDDLE_SEPARATOR_LINE and
                line.startswith('    ')):
            title = line.lstrip('    ')
            log_line_state = LogState.TITLE_LINE

            if idx < len(git_log_output_lines):
                continue

            commit_status = _validate_commit(
                commit_sha1, merge, author, committer, title, separator, body)

            commit_info[commit_sha1] = commit_status

        # Blank line between title and body (still contains 4 space prefix)
        if log_line_state == LogState.TITLE_LINE and line.startswith('    '):
            separator = line.lstrip('    ')
            log_line_state = LogState.BLANK_LINE

            if idx < len(git_log_output_lines):
                continue

            commit_status = _validate_commit(
                commit_sha1, merge, author, committer, title, separator, body)

            commit_info[commit_sha1] = commit_status

        # Body lines
        if (
                log_line_state in [LogState.BLANK_LINE, LogState.BODY_LINES] and
                line.startswith('    ')):
            body.append(line.lstrip('    '))
            log_line_state = LogState.BODY_LINES

            if idx < len(git_log_output_lines):
                continue

            commit_status = _validate_commit(
                commit_sha1, merge, author, committer, title, separator, body)

            commit_info[commit_sha1] = commit_status

        # End of commit message
        if (
                log_line_state in [
                    LogState.TITLE_LINE, LogState.BLANK_LINE,
                    LogState.BODY_LINES] and
                line == ''):

            commit_status = _validate_commit(
                commit_sha1, merge, author, committer, title, separator, body)

            commit_info[commit_sha1] = commit_status

            log_line_state = LogState.SEPARATOR_LINE
            commit_sha1 = None
            merge = None
            author = None
            committer = None
            title = None
            separator = None
            body = []

    return commit_info



    # Even if one or more commits are marked as failed in a branch that's
    # pull requested, Github will still consider the branch to be in a good
    # state if the head commit is not marked as failed.  To get around
    # this, if any of the commits in the branch are marked as failed, we
    # mark the head commit the same way (even if there's nothing else wrong
    # with it).
    if branch_status_set:
        status_url = (
            '{endpoint}/repos/{org}/{repo}/commits/{sha1}/'
            'statuses'.format(
                endpoint=GITHUB_API_ENDPOINT, org=org_name,
                repo=repo_name, sha1=head_sha1))
        http_auth = auth=requests.auth.HTTPBasicAuth(
            USERNAME, PERSONAL_ACCESS_TOKEN)

        responses_json = requests.api.get(status_url, auth=http_auth)
        responses = responses_json.json()

        status_set = False
        for response in responses:
            if (
                    response['context'] == 'gitbot' and
                    response['state'] == 'failure'):
                status_set = True
                break

        # If status is already set, then we don't need to set it again
        if not status_set:
            post_body = {
                'state': 'failure',
                'context': 'gitbot',
                'description': 'Branch contains commits in failure state'
            }

            # Set the status for this commit
            response = requests.api.post(
                status_url, json=post_body, auth=http_auth)


# TODO:
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
        head_sha1 = request_data['pull_request']['head']['sha']


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

            # Sleep for a second before running the git fetch command
            time.sleep(1)
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
                'refs/heads/{base_branch_name}'.format(
                    github_hostname=GITHUB_HOSTNAME,
                    org_repo_name=org_repo_name,
                    base_branch_name=base_branch_name))

            # sleep for a second before running the git fetch command
            time.sleep(1)
            subprocess.call(fetch_base_cmd)

            log_start_ref = 'FETCH_HEAD'
            log_end_ref = (
                'refs/heads/{org}/{repo}/PR/{pr_number}/{base_branch_name}/'
                'rebase-head/0'.format(
                    org=org_name, repo=repo_name, pr_number=pr_number,
                    base_branch_name=base_branch_name))
            commit_info = _parse_commit_log(log_start_ref, log_end_ref)

            branch_status_set = False
            for sha1, errors in commit_info.items():
                # If there are no issues with the commit, then skip it
                if errors == []:
                    continue

                # Check the status of the commit
                status_url = (
                    '{endpoint}/repos/{org}/{repo}/commits/{sha1}/'
                    'statuses'.format(
                        endpoint=GITHUB_API_ENDPOINT, org=org_name,
                        repo=repo_name, sha1=sha1))
                http_auth = auth=requests.auth.HTTPBasicAuth(
                    USERNAME, PERSONAL_ACCESS_TOKEN)

                responses_json = requests.api.get(status_url, auth=http_auth)
                responses = responses_json.json()

                status_set = False
                for reponse in responses:
                    if (
                            response['context'].startswith('gitbot') and
                            response['state'] == 'failure'):
                        status_set = True
                        branch_status_set = True
                        break

                # If status is already set, we don't need to set it again
                if status_set:
                    continue

                branch_status_set = True
                post_body = {
                    'state': 'failure',
                }

                # If there are a number of issues with the commit, then it's
                # best to post a separate status for each of them.  If the
                # description line gets too long, then the status doesn't get
                # set like it should.
                for error in errors:
                    time.sleep(1)
                    post_body['context'] = 'gitbot-{context}'.format(context=error[0])
                    post_body['description'] = error[1]

                    # Set the status for this commit
                    response = requests.api.post(status_url, json=post_body, auth=http_auth)

            # Even if one or more commits are marked as failed in a branch that's
            # pull requested, Github will still consider the branch to be in a good
            # state if the head commit is not marked as failed.  To get around
            # this, if any of the commits in the branch are marked as failed, we
            # mark the head commit the same way (even if there's nothing else wrong
            # with it).
            if branch_status_set:
                print 'branch status is set'
                status_url = (
                    '{endpoint}/repos/{org}/{repo}/commits/{sha1}/'
                    'statuses'.format(
                        endpoint=GITHUB_API_ENDPOINT, org=org_name,
                        repo=repo_name, sha1=head_sha1))
                http_auth = auth=requests.auth.HTTPBasicAuth(
                    USERNAME, PERSONAL_ACCESS_TOKEN)

                responses_json = requests.api.get(status_url, auth=http_auth)
                responses = responses_json.json()

                status_set = False
                for response in responses:
                    if (
                            response['context'].startswith('gitbot') and
                            response['state'] == 'failure'):
                        status_set = True
                        break

                # If status is already set, then we don't need to set it again
                if not status_set:
                    post_body = {
                        'state': 'failure',
                        'context': 'gitbot-branch-check',
                        'description': 'Branch contains commits in failure state'
                    }

                    # Set the status for this commit
                    response = requests.api.post(
                        status_url, json=post_body, auth=http_auth)

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

        # Sleep for a second before calling ls-remote since Github will
        # sometimes send webhook events before it updates its git remote
        # endpoint branch head information
        time.sleep(1)
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
        time.sleep(1)
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
                sender, url_root, base_branch_name, latest_rebase)

            # Post the comment on the Github PR
            post_url = (
                '{endpoint}/repos/{org}/{repo}/issues/{pr_number}/'
                'comments'.format(
                    endpoint=GITHUB_API_ENDPOINT, org=org_name, repo=repo_name,
                    pr_number=pr_number))
            post_body = {'body': comment}
            http_auth = auth=requests.auth.HTTPBasicAuth(
                USERNAME, PERSONAL_ACCESS_TOKEN)

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
            'git fetch git@{github_hostname}:{org_name}/{repo_name}.git '
            'refs/heads/{base_branch_name}'.format(
                github_hostname=GITHUB_HOSTNAME,
                org_name=org_name, repo_name=repo_name,
                base_branch_name=base_branch_name))
        # Sleep for a second before issuing the git fetch command
        time.sleep(1)
        subprocess.call(fetch_base_cmd)

        log_start_ref = 'FETCH_HEAD'
        log_end_ref = local_branch_name
        commit_info = _parse_commit_log(log_start_ref, log_end_ref)

        branch_status_set = False
        for sha1, errors in commit_info.items():

            # If this commit has no issues, then move onto the next one
            if errors == []:
                continue

            # Check the status of the commit
            status_url = (
                '{endpoint}/repos/{org}/{repo}/commits/{sha1}/'
                'statuses'.format(
                    endpoint=GITHUB_API_ENDPOINT, org=org_name,
                    repo=repo_name, sha1=sha1))
            http_auth = auth=requests.auth.HTTPBasicAuth(
                USERNAME, PERSONAL_ACCESS_TOKEN)

            responses_json = requests.api.get(status_url, auth=http_auth)
            responses = responses_json.json()

            status_set = False
            for response in responses:
                if (
                        response['context'].startswith('gitbot') and
                        response['state'] == 'failure'):
                    status_set = True
                    branch_status_set = True
                    break

            # If status is already set, then we don't need to set it again
            if status_set:
                continue

            branch_status_set = True
            post_body = {
                'state': 'failure',
            }

            # If there are a number of issues with the commit, then it's best
            # to post a separate status for each of them.  If the description
            # line gets too long, then the status doesn't get set like
            # it should.
            for error in errors:
                time.sleep(1)
                print error
                post_body['context'] = 'gitbot-{context}'.format(context=error[0])
                post_body['description'] = error[1]

                # Set the status for this commit
                response = requests.api.post(
                    status_url, json=post_body, auth=http_auth)

        # Even if one or more commits are marked as failed in a branch that's
        # pull requested, Github will still consider the branch to be in a good
        # state if the head commit is not marked as failed.  To get around
        # this, if any of the commits in the branch are marked as failed, we
        # mark the head commit the same way (even if there's nothing else wrong
        # with it).
        if branch_status_set:
            status_url = (
                '{endpoint}/repos/{org}/{repo}/commits/{sha1}/'
                'statuses'.format(
                    endpoint=GITHUB_API_ENDPOINT, org=org_name,
                    repo=repo_name, sha1=sha_after))
            http_auth = auth=requests.auth.HTTPBasicAuth(
                USERNAME, PERSONAL_ACCESS_TOKEN)

            responses_json = requests.api.get(status_url, auth=http_auth)
            responses = responses_json.json()

            status_set = False
            for response in responses:
                if (
                        response['context'].startswith('gitbot') and
                        response['state'] == 'failure'):
                    status_set = True
                    break

            # If status is already set, then we don't need to set it again
            if not status_set:
                post_body = {
                    'state': 'failure',
                    'context': 'gitbot-branch-check',
                    'description': 'Branch contains commits in failure state'
                }

                # Set the status for this commit
                response = requests.api.post(
                    status_url, json=post_body, auth=http_auth)

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
            'git fetch git@{github_hostname}:{org}/{repo}.git '
            # 'git fetch ../git-rebase.git '
            'refs/heads/{base_branch}'.format(
                github_hostname=GITHUB_HOSTNAME, org=org, repo=repo,
                base_branch=base_branch))
        time.sleep(1)
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
        return_str = str(html_parser)

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
    time.sleep(1)
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
        'git fetch git@{github_hostname}:{org}/{repo}.git '
        #'git fetch ../git-rebase.git '
        'refs/heads/{base_branch}'.format(
            github_hostname=GITHUB_HOSTNAME, org=org, repo=repo,
            base_branch=base_branch))
    time.sleep(1)
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
        'git fetch git@{github_hostname}:{org}/{repo}.git '
        #'git fetch ../git-rebase.git '
        'refs/heads/{base_branch}'.format(
            github_hostname=GITHUB_HOSTNAME, org=org, repo=repo,
            base_branch=base_branch))
    time.sleep(1)
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
    app.run('10.4.20.98', 8000)
