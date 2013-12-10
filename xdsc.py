#!/usr/bin/python

import errno
import logging
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from email.mime.text import MIMEText
from optparse import OptionParser
from xml.etree import ElementTree

from gi.repository import Gtk, GObject, Pango, GLib, Gdk, Gio

import discuss

dslogger = logging.getLogger('xdsc.discuss')
uilogger = logging.getLogger('xdsc.ui')

DEFAULT_UI_FILE="/usr/share/debathena-xdsc/xdsc.ui"
DEFAULT_ICON_FILE="/usr/share/debathena-xdsc/xdsc_icon.gif"
DEFAULT_DISCUSS_EDITOR="gedit"
DEFAULT_TIMEOUT=3

def gtk_tree_model_get_iter_last(tree_model):
    """
    Convenience function to return a GtkTreeIter on the last row of the model.
    """
    path_row = 0
    if len(tree_model):
        path_row = len(tree_model) - 1
    tree_path = Gtk.TreePath.new_from_string(str(path_row))
    tree_iter = tree_model.get_iter(tree_path)

class DiscussWrapper:
    """
    Abstraction for the discuss interface.
    """

    reply_action = "Replying to {trn.meeting.short_name}[{trn.current}]..."
    new_action = "New transaction in '{mtg.long_name}'..."

    reply_header = "\n\nOn {date}, {trn.author} ({signature}) said in " \
        "{trn.meeting.short_name}[{trn.current}]:\n"

    transaction_header = """\
### xdsc: Enter your transaction on the lines below these instructions.
### xdsc: When you're done, save the file, and quit this editor.
### xdsc: An empty file will abort the operation and no post will be created.
### xdsc: These instructions will be removed before posting.
### xdsc:
### xdsc: {action}
### xdsc:
### xdsc: Begin your transaction on the line below this one.

"""

    def __init__(self, timeout):
        self.meetingcache = {}
        self.connectioncache = {}
        self.timeout = timeout
        self.rcfile = discuss.RCFile()
        self.meetings = self.rcfile.entries

    def _get_connection(self, host):
        if host not in self.connectioncache:
            conn = discuss.Client(host, timeout=self.timeout)
            self.connectioncache[host] = conn
        return self.connectioncache[host]

    def get_meeting(self, name):
       """
       Load the meeting, cache it, and return it.
       N.B. We can't cache connections (discuss.Client) otherwise
            hilarity ensues.
       """
       dslogger.debug('get_meeting %s', name)
       location = self.rcfile.lookup(name)
       if location not in self.meetingcache:
           mtg = discuss.Meeting(self._get_connection(location[0]),
                                 location[1])
           try:
               mtg.check_update(0)
           except discuss.client.DiscussError as e:
               # Because transaction 0 could have been deleted.
               # But we can't actually check mtg.lowest because if
               # the meeting doesn't exist, load_info() will fail.
               if e.code != discuss.constants.NO_SUCH_TRN:
                   raise
           mtg.load_info()
           self.meetingcache[location] = mtg
       return self.meetingcache[location]

    def add_meeting(self, host, path):
        cli = discuss.Client(host)
        mtg = discuss.Meeting(cli, path)
        self.rcfile.add(mtg)
        self.rcfile.save()

    def delete_meeting(self, meeting):
        mtg = self.rcfile.lookup(meeting)
        if mtg is None:
            raise ValueError(
                '"{0}" is not in your .meetings file.'.format(meeting)
                )
        del self.rcfile.entries[mtg]
        self.rcfile.save()
        self.rcfile.recache()

    def _last_read_transaction(self, meeting_obj):
        lookup = self.rcfile.lookup(meeting_obj.long_name)
        entry = self.rcfile.entries[lookup]
        return entry['last_transaction']

    def meeting_has_changed(self, meeting_obj):
        last_trans = self._last_read_transaction(meeting_obj)
        return meeting_obj.check_update(last_trans)

    def touch_meeting(self, meeting_obj, trn_obj):
        self.rcfile.touch(meeting_obj.id, trn_obj.current)

    def get_transaction(self, meeting_obj, trn_num, updateLast=True):
        dslogger.debug("Retrieving %s[%d]", meeting_obj.short_name, trn_num)
        if trn_num < meeting_obj.lowest or trn_num > meeting_obj.highest:
            raise ValueError("Transaction number out of range for meeting.")
        trn_obj = meeting_obj.get_transaction(trn_num)
        if updateLast:
            self.touch_meeting(meeting_obj, trn_obj)
        return trn_obj

    def find_next_valid_transaction(self, meeting_obj, backwards=False):
        start_from = self._last_read_transaction(meeting_obj)
        if not backwards and start_from >= meeting_obj.last:
            self.go_to_transaction(meeting_obj.last)
        if backwards and start_from <= meeting_obj.first:
            self.go_to_transaction(meeting_obj.first)
        end = meeting_obj.first if backwards else meeting_obj.last
        increment = -1 if backwards else 1
        for t in xrange(start_from + increment,
                        end + increment,
                        increment):
            dslogger.debug("Trying transaction #%d in %s",
                           t, meeting_obj.long_name)
            try:
                return self.get_transaction(meeting_obj, t)
            except discuss.DiscussError as err:
                if err.code in (discuss.constants.DELETED_TRN,
                                discuss.constants.NO_SUCH_TRN):
                    continue
                else:
                    raise

    @staticmethod
    def format_transaction_for_list(trn):
        assert trn is not None
        line = u" [{trn.number}]{flags} {num_lines:>6}" \
               u" {date} {author:<16} {subject:.60}"
        markup = line.format(trn=trn,
                             flags='F' if trn.flags else u' ',
                             author=trn.author.split('@')[0],
                             num_lines="({0})".format(trn.num_lines),
                             subject=unicode(trn.subject, errors='replace'),
                             date=trn.date_entered.strftime("%m/%d/%y %H:%M"))
        return GLib.markup_escape_text(markup.encode('UTF-8', errors='ignore'))

    @staticmethod
    def format_transaction(trn):
        assert trn is not None
        text = u''
        header = u"[{trn.number}]{flags} {trn.author} ({signature}) " \
            u"{trn.meeting.long_name} {date} ({trn.num_lines} lines)\n" \
            u"Subject: {subject}\n"
        footer = u"--[{trn.number}]--\n\n"
        text += header.format(trn=trn,
                              flags='F' if trn.flags else ' ',
                              signature=unicode(trn.signature,
                                                errors='replace'),
                              subject=unicode(trn.subject, errors='replace'),
                              date=trn.date_entered.strftime("%m/%d/%y %H:%M"))
        text += unicode(trn.get_text(), errors='replace')
        text += footer.format(trn=trn)
        return text

class Xdsc:
    # A mapping of widget id to font description string
    monospace_font_widgets = ('help_textview',
                              'transaction_textview',
                              'upper_treeview')

    # A mapping of menus to the buttons they pop down from
    # for positioning.
    menubuttons_map = {'configure_menu': 'configure_button',
                       'mode_menu': 'mode_button',
                       'show_menu': 'show_button',
                       'goto_menu': 'goto_button',
                       'enter_menu': 'enter_button',
                       'write_menu': 'write_button'}

    def __init__(self, ui_file, icon_file, dsc_wrapper):
        self.builder = Gtk.Builder()
        try:
            self.builder.add_from_file(ui_file)
        except GLib.GError as e:
            sys.exit("Unable to load UI file: " + str(e))
        # GtkBuilder still scribbles over the id/name properties
        # We could get away without this by doing things in the handlers
        # like "if self.builder.get_object(name) is widget: [...]" but meh
        for object_id in [x.get('id') for x in
                        ElementTree.parse(ui_file).findall('.//object')]:
            if isinstance(self.builder.get_object(object_id), Gtk.Widget):
                self.builder.get_object(object_id).set_name(object_id)
        # Set some font defaults
        for widget_id in self.monospace_font_widgets:
            gsettings = Gio.Settings('org.gnome.desktop.interface')
            try:
                font_name = gsettings['monospace-font-name']
            except KeyError:
                font_name = 'Courier'
            pangofont = Pango.FontDescription(font_name)
            pangofont.set_size(9 * Pango.SCALE)
            self.builder.get_object(widget_id).override_font(pangofont)
        self.builder.connect_signals(self)
        self.main_window = self.builder.get_object('xdsc_main_window')
        try:
            self.main_window.set_icon_from_file(icon_file)
        except GLib.GError as e:
            print >>sys.stderr, "Failed to set icon file for window: ", e
        self.upper_treeview = self.builder.get_object('upper_treeview')
        self.meeting_liststore = self.builder.get_object("meeting_list_store")
        self.trans_liststore = self.builder.get_object("transaction_list_store")
        # The 'Show' button is not valid in meeting mode
        self.builder.get_object('show_button').set_sensitive(False)
        self.discuss = dsc_wrapper
        dlg = Gtk.MessageDialog(self.main_window, 0,
                                Gtk.MessageType.INFO,
                                Gtk.ButtonsType.NONE,
                                "Loading meeting information, please wait...")
        dlg.show()
        while Gtk.events_pending():
            Gtk.main_iteration()
        self.check_meetings()
        self.current_meeting = None
        self.current_transaction = None
        self.update_meeting_list()
        dlg.destroy()
        self.upper_treeview.grab_focus()

    def check_meetings(self):
        """
        Ensure that the long names in the file match what the server
        thinks they are.  They can -- and do -- change.  The short
        names cannot change without the path changing, which would
        render the meeting useless.  This also has the benefit that
        we visit every meeting and remove invalid values.
        """
        update = False
        for m in self.discuss.meetings:
            if self.discuss.meetings[m]['deleted']:
                continue
            try:
                mtg = self.discuss.get_meeting(m)
            except socket.timeout as e:
                self.msg_dialog("Unable to attend '{0}' on '{1}' ({2}).\n"
                                "It will be flagged as 'deleted' in your "
                                "meetings file.  You may wish to clean up the "
                                "file later.",
                                self.discuss.meetings[m]['displayname'],
                                self.discuss.meetings[m]['hostname'],
                                e, warn=True)
                self.discuss.meetings[m]['deleted'] = True
                update = True
                continue
            except discuss.client.DiscussError as e:
                dslogger.debug('%s: %s', ':'.join(m), e)
                if e.code == discuss.constants.NO_SUCH_MTG:
                    self.msg_dialog("Meeting '{0}' on '{1}' no longer exists. "
                                    "It will be flagged as 'deleted' in your "
                                    "meetings file.  You may wish to clean up "
                                    "the file later.",
                                    self.discuss.meetings[m]['displayname'],
                                    self.discuss.meetings[m]['hostname'],
                                    e, warn=True)
                    self.discuss.meetings[m]['deleted'] = True
                    update = True
                continue
                # We catch these errors when populating the liststore
            except discuss.rpc.ProtocolError as e:
                dslogger.debug('%s: %s', ':'.join(m), e)
                # We catch these errors when populating the liststore
                continue
            if mtg.long_name != self.discuss.meetings[m]['names'][0]:
                dslogger.debug("Updating long name from '%s' to '%s'",
                               self.discuss.meetings[m]['names'][0],
                               mtg.long_name)
                update = True
                self.discuss.meetings[m]['names'][0] = mtg.long_name
        if update:
            self.discuss.rcfile.save()
            self.discuss.rcfile.recache()

    def quit(self):
        """
        Quit the application, ideally saving the RC file.
        """
        self.discuss.rcfile.save()
        Gtk.main_quit()

    def in_transaction_mode(self):
        """
        Return True if in transaction mode, else meeting mode
        """
        return self.upper_treeview.get_model() is self.trans_liststore

    def change_meeting(self, meeting_obj):
        if self.current_meeting is meeting_obj:
            return True
        self.trans_liststore.clear()
        self.builder.get_object('transaction_buffer').set_text('')
        self.builder.get_object('next_chain_button').set_sensitive(False)
        self.builder.get_object('prev_chain_button').set_sensitive(False)
        self.builder.get_object('write_button').set_sensitive(False)
        self.current_meeting = meeting_obj
        last_trans = self.discuss._last_read_transaction(self.current_meeting)
        try:
            self.current_transaction = self.discuss.get_transaction(
                self.current_meeting, last_trans)
        except (ValueError, discuss.DiscussError) as e:
            dslogger.debug('Error: %s', e)
            errdetail = "Something bad happened."
            if isinstance(e, ValueError):
                errdetail = "The last read transaction ({0}) in {1} was " \
                    "outside the range for the meeting."
            elif e.code == discuss.constants.DELETED_TRN:
                errdetail =  "The last read transaction ({0}) in {1} has "\
                    "been deleted."
            elif e.code == discuss.constants.NO_SUCH_TRN:
                errdetail =  "The last read transaction ({0}) in {1} does "\
                    "not exist."
            err = errdetail + \
                " Your current transaction will be updated " \
                "to the next unread one, or the last transaction in " \
                "the meeting if there are no more unread transactions."
            self.msg_dialog(err, last_trans,
                            self.current_meeting.long_name, info=True)
            self.display_transaction(
                self.discuss.find_next_valid_transaction(
                    self.current_meeting), by_object=True)
        self.builder.get_object('enter_reply').set_sensitive(
            'a' in self.current_meeting.access_modes)
        self.builder.get_object('enter_new_transaction').set_sensitive(
            'w' in self.current_meeting.access_modes)
        self.builder.get_object('enter_button').set_sensitive(
            self.builder.get_object('enter_reply').get_sensitive() or
            self.builder.get_object('enter_new_transaction').get_sensitive())
        self.update_status_label(mtg=self.current_meeting)

    def update_meeting_list(self):
        uilogger.debug('Clearing liststore')
        self.meeting_liststore.clear()
        uilogger.debug('Cleared!')
        meetings = self.discuss.meetings
        for m in meetings:
            if meetings[m]['deleted']:
                continue
            try:
                mtg = self.discuss.get_meeting(m)
            except (discuss.rpc.ProtocolError,
                    discuss.client.DiscussError,
                    socket.timeout) as e:
                self.msg_dialog("Error while attending {0}:\n{1}\n\n"
                                "The meeting will be temporarily removed from "
                                "your list of meetings.",
                                meetings[m]['displayname'],
                                e)
                continue
            updated = False
            display_name = ', '.join(meetings[m]['names'])
            if self.discuss.meeting_has_changed(mtg):
                updated = True
                display_name = "<b>%s</b>" % (display_name,)
            self.meeting_liststore.append((display_name,
                                           mtg))
        uilogger.debug('%d meetings loaded', len(self.meeting_liststore))
        for widget_id in ('down_button', 'up_button', 'mode_transactions',
                          'next_button', 'prev_button', 'goto_button',
                          'enter_button', 'write_button'):
            self.builder.get_object(widget_id).set_sensitive(
                len(self.meeting_liststore) > 0)
        if len(self.meeting_liststore) < 1:
            self.msg_dialog("No meetings to display.", warn=True)
        else:
            next_unread_meeting_path = self._find_unread_meetings()
            if next_unread_meeting_path is not None:
                self.upper_treeview.set_cursor(next_unread_meeting_path, None)
            else:
                self.upper_treeview.set_cursor(Gtk.TreePath.new_first())
                self.msg_dialog("Nothing more to read.", info=True)

    def remove_temporary_file(self, filename):
        try:
            os.unlink(filename)
        except OSError as e:
            self.msg_dialog("Unable to remove '{0}': {1}", filename, e)

    def post_reply(self, replying_to=None):
        editor = os.getenv('DISCUSS_EDITOR', DEFAULT_DISCUSS_EDITOR)
        filename = None
        try:
            with tempfile.NamedTemporaryFile(prefix='xdsc', delete=False) as f:
                filename = f.name
                action = DiscussWrapper.new_action.format(
                    mtg=self.current_meeting)
                if replying_to is not None:
                    action = DiscussWrapper.reply_action.format(trn=replying_to)
                f.write(DiscussWrapper.transaction_header.format(action=action))
                if replying_to is not None:
                    f.write(DiscussWrapper.reply_header.format(
                            date=replying_to.date_entered.strftime(
                                "%m/%d/%y %H:%M"),
                            signature=unicode(replying_to.signature,
                                              errors='replace'),
                            trn=replying_to))
                    f.write("> ")
                    f.write(replying_to.get_text().replace("\n", "\n> "))
        except IOError as e:
            self.msg_dialog("Unable to create temporary file '{0}': {1}",
                             filename, e)
            return None
        if filename is None:
            # Shouldn't happen.
            self.msg_dialog("Could not determine temporary file name!")
            return None
        rv = None
        try:
            rv = subprocess.call([editor, filename])
        except OSError as e:
            self.msg_dialog("Unable to launch editor ({0}): {1}",
                            editor, e)
            self.remove_temporary_file(filename)
            return None
        if rv != 0:
            if self.msg_dialog("Your editor indicated an error (exited with "
                               "non-zero status).  That's probably bad.  "
                               "Should I delete the temporary file ({0})?",
                               filename, question=True):
                self.remove_temporary_file(filename)
            return None
        body = ''
        try:
            with open(filename, 'r') as f:
                body = ''.join([l for l in f.readlines()
                                if not l.startswith('### xdsc:')])
        except IOError as e:
            self.msg_dialog("Unable to read temporary file '{0}': {1}",
                             filename, e)
            return None
        if len(body.strip()) == 0:
            self.msg_dialog("Empty transaction detected.  Posting aborted.")
            self.remove_temporary_file(filename)
            return None
        dlg = self.builder.get_object('enter_transaction_dlg')
        self.builder.get_object('enter_transaction_ok').set_sensitive(False)
        subj_entry = self.builder.get_object('enter_transaction_subject')
        sig_entry = self.builder.get_object('enter_transaction_signature')
        try:
            default_sig = pwd.getpwuid(os.getuid()).pw_gecos.split(',')[0]
        except:
            default_sig = 'Unknown User'
        sig_entry.set_text(default_sig)
        if replying_to is not None:
            subj = u"Re: " + unicode(replying_to.subject, errors='replace')
            subj_entry.set_text(subj)
        else:
            subj_entry.set_text('')
        subj_entry.grab_focus()
        response = dlg.run()
        dlg.hide()
        if response == Gtk.ResponseType.CANCEL:
            if self.msg_dialog('Delete temporary file ({0})?',
                               filename, question=True):
                self.remove_temporary_file(filename)
        else:
            new_trn = self.current_meeting.post(body,
                                                subj_entry.get_text().strip(),
                                                sig_entry.get_text().strip(),
                                                0 if replying_to is None
                                                else replying_to.current)
            self.current_meeting.load_info(force=True)
            self.display_transaction(new_trn, by_object=True)
            self.remove_temporary_file(filename)

    def can_send_email(self):
        """
        Sanity-check the obvious failures when sending e-mail. This is not
        designed to be foolproof, it's designed to prevent dumb typos.
        """
        to = self.builder.get_object('send_email_to').get_text().strip()
        sender = self.builder.get_object('send_email_from').get_text().strip()
        subj = self.builder.get_object('send_email_subject').get_text().strip()
        return len(to) > 0 and len(subj) > 0 and len(sender) > 0 \
            and '@' in to and '@' in sender

    def send_email_validate(self, widget):
        self.builder.get_object('send_email_ok').set_sensitive(
            self.can_send_email())

    def enter_transaction_validate(self, widget):
        subj = self.builder.get_object(
            'enter_transaction_subject').get_text().strip()
        sig = self.builder.get_object(
            'enter_transaction_signature').get_text().strip()
        self.builder.get_object('enter_transaction_ok').set_sensitive(
            len(subj) > 0 and len(sig) > 0)

    def transaction_entry_changed(self, widget):
        self.builder.get_object('goto_transaction_dialog_ok').set_sensitive(
            len(widget.get_text().strip()) > 0)

    def transaction_entry_insert_text(self, widget, text, text_len,
                                      pointer, data=None):
        if text_len == 0:
            return True
        if re.search(r'\D', text):
            widget.error_bell()
            widget.set_icon_from_stock(Gtk.EntryIconPosition.SECONDARY,
                                       'gtk-dialog-warning')
            widget.set_icon_activatable(Gtk.EntryIconPosition.SECONDARY,
                                        False)
            widget.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY,
                                         "Only numbers are allowed")
            widget.stop_emission('insert-text')
        else:
            widget.set_icon_from_stock(Gtk.EntryIconPosition.SECONDARY,
                                       None)
        return True

    def msg_dialog(self, *args, **kwargs):
        errortext = args[0]
        if len(args) > 1:
            errortext = errortext.format(*args[1:])
        buttons = Gtk.ButtonsType.OK
        if kwargs.get('fatal', False):
            buttons = Gtk.ButtonsType.CLOSE
        dialogtype = Gtk.MessageType.ERROR
        if kwargs.get('warn', False):
            dialogtype = Gtk.MessageType.WARNING
        elif kwargs.get('info', False):
            dialogtype = Gtk.MessageType.INFO
        elif kwargs.get('question', False):
            dialogtype = Gtk.MessageType.QUESTION
            buttons = Gtk.ButtonsType.YES_NO
        dialog = Gtk.MessageDialog(self.main_window, 0,
                                   dialogtype, buttons, errortext)
        dialog.set_title(kwargs.get('title', 'Xdsc'))
        response = dialog.run()
        dialog.destroy()
        if buttons == Gtk.ButtonsType.YES_NO:
            return response == Gtk.ResponseType.YES
        else:
            return None

    def transactions_callback(self, cur, total, left):
        self.update_status_label(remaining=left)
        while Gtk.events_pending():
            Gtk.main_iteration()

    def load_more_transactions(self, num):
        trn = self.trans_liststore.get_value(
            self.trans_liststore.get_iter_first(), 1)
        uilogger.debug("loading %d more transaction(s)", num)
        for _ in xrange(0, num):
            if trn.prev == 0:
                uilogger.debug("Hit start of meeting")
                break
            trn = self.current_meeting.get_transaction(trn.prev)
            self.trans_liststore.prepend(
                (DiscussWrapper.format_transaction_for_list(trn), trn))
            uilogger.debug("prepending %d", trn.current)
            while Gtk.events_pending():
                Gtk.main_iteration()

    def _select_transaction_by_num(self, trn_num,
                                   backwards=False, start_path=None):
        uilogger.debug("selecting %d by number", trn_num)
        ls = self.trans_liststore
        tree_iter = ls.get_iter_first()
        if start_path is not None:
            tree_iter = ls.get_iter(start_path)
        elif backwards:
            tree_iter = gtk_tree_model_get_iter_last(ls)
        walk_function = ls.iter_previous if backwards else ls.iter_next
        while tree_iter is not None:
            if ls.get_value(tree_iter, 1).current == trn_num:
                self.upper_treeview.set_cursor(ls.get_path(tree_iter), None)
                break
            tree_iter = walk_function(tree_iter)

    def display_transaction(self, num_or_obj, by_object=False):
        try:
            if by_object:
                trn = num_or_obj
            else:
                trn = self.discuss.get_transaction(self.current_meeting,
                                                   num_or_obj)
            text = DiscussWrapper.format_transaction(trn)
            self.mark_current_meeting_as_changed()
            self.current_transaction = trn
            self.builder.get_object('next_chain_button').set_sensitive(
                trn.nref != 0)
            self.builder.get_object('prev_chain_button').set_sensitive(
                trn.pref != 0)
            self.builder.get_object('write_button').set_sensitive(True)
            self.builder.get_object('transaction_buffer').set_text(text)
            self.update_status_label(mtg=self.current_meeting, trn=trn)
            self.builder.get_object('transaction_textview').grab_focus()
        except (ValueError, discuss.DiscussError) as e:
            self.msg_dialog(e)

    def _find_unread_meetings(self, backwards=False, start_path=None):
        tree_iter = self.meeting_liststore.get_iter_first()
        if start_path is not None:
            tree_iter = self.meeting_liststore.get_iter(start_path)
        elif backwards:
            tree_iter = gtk_tree_model_get_iter_last(self.meeting_liststore)
        walk_function = self.meeting_liststore.iter_next
        if backwards:
            walk_function = self.meeting_liststore.iter_previous
        while walk_function(tree_iter) is not None:
            tree_iter = walk_function(tree_iter)
            if self.discuss.meeting_has_changed(
                    self.meeting_liststore.get_value(tree_iter, 1)):
                return self.meeting_liststore.get_path(tree_iter)
        return None

    def mark_current_meeting_as_changed(self):
        tree_iter = self.meeting_liststore.get_iter_first()
        while tree_iter is not None:
            markup = self.meeting_liststore.get_value(tree_iter, 0)
            mtg = self.meeting_liststore.get_value(tree_iter, 1)
            if mtg is self.current_meeting:
                if '<b>' in markup and not self.discuss.meeting_has_changed(
                        self.current_meeting):
                    self.meeting_liststore.set_value(
                        tree_iter, 0, ', '.join(
                            (self.current_meeting.long_name,
                             self.current_meeting.short_name)))
                    break
            tree_iter = self.meeting_liststore.iter_next(tree_iter)

    def update_status_label(self, **kwargs):
        if 'remaining' in kwargs:
            text = "Retrieving headers, please wait... ({0} remaining)"
            text = text.format(kwargs['remaining'])
        elif 'mtg' in kwargs:
            text = "Reading {mtg.long_name} [{mtg.first}-{mtg.last}]"
            if 'trn' in kwargs:
                text += ", #{trn.current}"
            text = text.format(**kwargs)
        self.builder.get_object("status_label").set_text(text)

    # Event handlers

    def xdsc_main_window_delete_event(self, widget, event, data=None):
        if self.msg_dialog("Quit the application?", question=True):
            self.quit()
        else:
            # Returning "True" inhibits the event
            return True

    def font_size_keypress_event(self, widget, event, data=None):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            if event.keyval == Gdk.KEY_plus or event.keyval == Gdk.KEY_KP_Add:
                pangofont = widget.get_style().font_desc
                if pangofont.get_size() // Pango.SCALE >= 16:
                    widget.error_bell()
                    return True
                pangofont.set_size(pangofont.get_size() + Pango.SCALE)
                uilogger.debug('Increasing font size of %s to %d (%d pt)',
                               widget.get_name(),
                               pangofont.get_size(),
                               pangofont.get_size() // Pango.SCALE)
                widget.override_font(pangofont)
            elif event.keyval == Gdk.KEY_minus or \
                    event.keyval == Gdk.KEY_KP_Subtract:
                pangofont = widget.get_style().font_desc
                if pangofont.get_size() // Pango.SCALE <= 6:
                    widget.error_bell()
                    return True
                pangofont.set_size(pangofont.get_size() - Pango.SCALE)
                uilogger.debug('Decreasing font size of %s to %d (%d pt)',
                               widget.get_name(),
                               pangofont.get_size(),
                               pangofont.get_size() // Pango.SCALE)
                widget.override_font(pangofont)

    def upper_treeview_cursor_changed(self, tree_view):
        uilogger.debug("tree_view_cursor_changed_handler")
        tree_row = tree_view.get_cursor()[0]
        uilogger.debug("tree_row=%s", tree_row)
        if tree_row is None:
            return True
        model = tree_view.get_model()
        tree_iter = model.get_iter(tree_row)
        if tree_iter is None:
            uilogger.debug("tree_iter is None, shouldn't happen")
            return True
        if self.in_transaction_mode():
            self.display_transaction(model.get_value(tree_iter, 1),
                                     by_object=True)
        else:
            self.change_meeting(model.get_value(tree_iter, 1))
        return True

    def upper_treeview_move_cursor(self, tree_view, gtk_movement_step,
                                   direction, data=None):
        uilogger.debug("move_cursor %s %s %d", gtk_movement_step,
                       tree_view.get_cursor()[0], direction)
        # Extend the liststore as we move up
        if direction != -1:
            return True
        tree_path = tree_view.get_cursor()[0]
        if tree_path != Gtk.TreePath.new_first():
            return True
        # We can't use Meeting.transactions() here because the range is unknown.
        # Basically, we want to load the previous 10 transactions. Some meetings
        # have huge gaps (hundreds of transactions) where spam was deleted.
        trn = self.current_transaction
        if trn.prev == 0:
            tree_view.error_bell()
            return True
        if gtk_movement_step == Gtk.MovementStep.DISPLAY_LINES:
            self.load_more_transactions(1)
            return True
        elif gtk_movement_step == Gtk.MovementStep.PAGES:
            # Figure out how many rows the view is currently displaying
            # and load that many more.
            (start_row, end_row) = [path.get_indices()[0] for path in
                                    self.upper_treeview.get_visible_range()]
            uilogger.debug("Visible rows from %d to %d", start_row, end_row)
            self.load_more_transactions(abs(end_row - start_row))
            return True

    # Menu handlers

    def configure_add_meeting_activate(self, menuitem, data=None):
        dialog = self.builder.get_object('add_meeting_dialog')
        self.builder.get_object('add_meeting_hostname').set_text('')
        self.builder.get_object('add_meeting_pathname').set_text(
            '/usr/spool/discuss/')
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                hostName = self.builder.get_object(
                    'add_meeting_hostname').get_text().strip()
                pathName = self.builder.get_object(
                    'add_meeting_pathname').get_text().strip()
                self.discuss.add_meeting(hostName, pathName)
                self.update_meeting_list()
            except (ValueError, discuss.DiscussError,
                    discuss.rpc.ProtocolError) as e:
                self.msg_dialog(str(e))
        dialog.hide()

    def configure_delete_meeting_activate(self, menuitem, data=None):
        dialog = self.builder.get_object('delete_meeting_dialog')
        if self.current_meeting is not None:
            self.builder.get_object('delete_meeting_meetingname').set_text(
                self.current_meeting.short_name)
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                meetingName = self.builder.get_object(
                    'delete_meeting_meetingname').get_text().strip()
                self.discuss.delete_meeting(meetingName)
                self.update_meeting_list()
            except (ValueError, discuss.DiscussError) as e:
                self.msg_dialog(str(e))
        dialog.hide()

    def mode_transactions_activate(self, widget, data=None):
        if self.current_meeting is None:
            self.msg_dialog("Not currently attending a meeting.",
                            warn=True)
            return None
        self.builder.get_object('show_button').set_sensitive(True)
        if len(self.trans_liststore) == 0:
            for trn in self.current_meeting.transactions(
                    self.current_transaction.current, -1,
                    self.transactions_callback):
                self.trans_liststore.append(
                    (DiscussWrapper.format_transaction_for_list(trn),
                     trn))
        self.upper_treeview.set_model(self.trans_liststore)
        self._select_transaction_by_num(self.current_transaction.current)
        self.upper_treeview.grab_focus()

    def mode_meetings_activate(self, widget, data=None):
        self.builder.get_object('show_button').set_sensitive(False)
        self.upper_treeview.set_model(self.meeting_liststore)

    def show_unread_activate(self, menuitem, data=None):
        self._select_transaction_by_num(
            self.discuss._last_read_transaction(self.current_meeting))

    def show_all_activate(self, menuitem, data=None):
        # Extend the transaction_liststore with all the transactions
        trn = self.trans_liststore.get_value(
            self.trans_liststore.get_iter_first(), 1)
        while trn.prev != 0:
            trn = self.current_meeting.get_transaction(trn.prev)
            self.trans_liststore.prepend(
                (DiscussWrapper.format_transaction_for_list(trn), trn))
            uilogger.debug("prepending %d", trn.current)
            self.update_status_label(
                remaining=trn.current - self.current_meeting.first)
            while Gtk.events_pending():
                Gtk.main_iteration()
        self.update_status_label(mtg=self.current_meeting,
                                 trn=self.current_transaction)

    def show_back10_activate(self, menuitem, data=None):
        uilogger.debug("Moving back 10")
        start_path = self.upper_treeview.get_visible_range()[0]
        visible_row = start_path.get_indices()[0]
        uilogger.debug("topmost visible row: %s", visible_row)
        if visible_row < 10:
            self.load_more_transactions(10 - visible_row)
        next_row = max(visible_row - 10, 0)
        uilogger.debug("new visible row will be: %s", next_row)
        self.upper_treeview.scroll_to_cell(
            Gtk.TreePath.new_from_string(str(next_row)), None, True, 0.0, 0.0)

    def goto_start_activate(self, widget, data=None):
        # All transactions have an fref and lref.  For single transactions,
        # these are the transaciton number
        self.display_transaction(self.current_transaction.fref)
        if self.in_transaction_mode():
            self.upper_treeview.get_selection().unselect_all()

    def goto_end_activate(self, widget, data=None):
        self.display_transaction(self.current_transaction.lref)
        if self.in_transaction_mode():
            self.upper_treeview.get_selection().unselect_all()

    def goto_first_activate(self, widget, data=None):
        self.display_transaction(self.current_meeting.first)
        if self.in_transaction_mode():
            self.upper_treeview.get_selection().unselect_all()

    def goto_last_activate(self, widget, data=None):
        self.display_transaction(self.current_meeting.last)
        if self.in_transaction_mode():
            self.upper_treeview.get_selection().unselect_all()

    def goto_number_activate(self, widget, data=None):
        dlg = self.builder.get_object('goto_transaction_dlg')
        entry = self.builder.get_object('transaction_number_entry')
        self.builder.get_object(
            'goto_transaction_dialog_ok').set_sensitive(False)
        entry.set_text('')
        entry.grab_focus()
        response = dlg.run()
        dlg.hide()
        if response == Gtk.ResponseType.OK:
            try:
                number = int(entry.get_text().strip())
                self.display_transaction(number)
                if self.in_transaction_mode():
                    self.upper_treeview.get_selection().unselect_all()
            except ValueError as e:
                self.msg_dialog("'{0}' is not a valid number.",
                                 entry.get_text())

    def enter_reply_activate(self, widget, data=None):
        self.post_reply(self.current_transaction)

    def enter_new_transaction_activate(self, widget, data=None):
        self.post_reply()

    def write_mail_to_someone_activate(self, widget, data=None):
        dlg = self.builder.get_object('send_email_dlg')
        username = os.getenv('USER', None)
        if username is not None:
            self.builder.get_object('send_email_from').set_text(
                '{0}@mit.edu'.format(username))
        default_subj = "{trn.meeting.short_name}[{trn.current}] {subject}"
        trn = self.current_transaction
        default_subj = default_subj.format(trn=trn,
                                           subject=unicode(trn.subject,
                                                           errors='replace'))
        self.builder.get_object('send_email_subject').set_text(default_subj)
        entry = self.builder.get_object('send_email_to')
        self.builder.get_object('send_email_ok').set_sensitive(False)
        entry.set_text('')
        entry.grab_focus()
        response = dlg.run()
        dlg.hide()
        if response == Gtk.ResponseType.OK:
            textbuffer = self.builder.get_object('transaction_buffer')
            start = textbuffer.get_start_iter()
            end = textbuffer.get_end_iter()
            msg = MIMEText(textbuffer.get_text(start, end, False))
            msg['To'] = entry.get_text().strip()
            msg['Subject'] = self.builder.get_object(
                'send_email_subject').get_text().strip()
            msg['From'] = self.builder.get_object(
                'send_email_from').get_text().strip()
            sendmail = subprocess.Popen(['/usr/sbin/sendmail', '-t'],
                                        stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
            (stdout, stderr) = sendmail.communicate(msg.as_string())
            if sendmail.returncode != 0:
                self.msg_dialog("An error occurred while sending e-mail: {0}",
                                stderr, warn=True)
            else:
                self.msg_dialog("E-mail sent successfully.",
                                info=True)

    def write_to_file_activate(self, widget, data=None):
        filedialog = Gtk.FileChooserDialog("Save transaction to file...",
                                           self.main_window,
                                           Gtk.FileChooserAction.SAVE,
                                           (Gtk.STOCK_CANCEL,
                                           Gtk.ResponseType.CANCEL,
                                           Gtk.STOCK_SAVE,
                                           Gtk.ResponseType.OK))
        filedialog.set_do_overwrite_confirmation(True)
        filename = "{0}_{1}.txt".format(self.discuss.current_meeting.short_name,
                                        self.current_transaction.current)
        filedialog.set_current_name(filename)
        response = filedialog.run()
        filedialog.hide()
        if response == Gtk.ResponseType.OK:
            textbuffer = self.builder.get_object('transaction_buffer')
            start = textbuffer.get_start_iter()
            end = textbuffer.get_end_iter()
            try:
                with open(filedialog.get_filename(), 'w') as f:
                    f.write(textbuffer.get_text(start, end, False))
            except IOError as e:
                self.msg_dialog(e)

    # Button handlers

    def up_down_button_clicked(self, widget, data=None):
        """
        Signal handler for the 'clicked' event for the 'Up' and 'Down'
        buttons in the top toolbar.  In meeting mode, advance to the
        next/previous meeting with unread transactions.  In transaction
        mode, advance to the next/previous transaction.
        """
        going_up = widget.get_name() == 'up_button'
        if self.in_transaction_mode():
            self.upper_treeview.emit('move-cursor',
                                     Gtk.MovementStep.DISPLAY_LINES,
                                     -1 if going_up else 1)
        else:
            curPath = self.upper_treeview.get_cursor()[0]
            meetingPath = self._find_unread_meetings(start_path=curPath,
                                                     backwards = going_up)
            if meetingPath is None:
                self.msg_dialog("No more meetings with unread transactions.",
                                warn=True)
            else:
                self.upper_treeview.set_cursor(meetingPath, None)
        return True

    def update_button_clicked(self, widget, data=None):
        """
        Signal handler for the 'clicked' event for the 'Update' button.
        """
        self.update_meeting_list()

    def get_menubutton_position(self, menu, toolbutton=None):
        """
        A GtkMenuPositionFunc callback for positioning the menus for the
        "menubuttons" directly under the buttons themselves.  Returns
        a tuple of the x and y coordinates, and 'True' indicating it
        should adjust the menu position if it would pop up outside the
        screen boundaries.
        """
        assert isinstance(toolbutton, Gtk.ToolButton)
        (xwin, ywin) = toolbutton.get_window().get_position()
        allocation_rect = toolbutton.get_allocation()
        return (xwin + allocation_rect.x,
                ywin+allocation_rect.y+allocation_rect.height,
                True)

    def menubutton_clicked(self, widget, data=None):
        """
        Handler for the 'clicked' event for the "menu buttons"
        ('Configure', 'Mode', 'Show', 'Goto', 'Enter', 'Write').
        The XML passes the correct menu as widget data for the object.
        """
        assert widget.get_name() in self.menubuttons_map
        button_id = self.menubuttons_map[widget.get_name()]
        widget.popup(None, None, self.get_menubutton_position,
                     self.builder.get_object(button_id), 0,
                     Gtk.get_current_event_time())

    def help_button_clicked(self, widget, data=None):
        dlg = self.builder.get_object('help_dialog')
        dlg.run()
        dlg.hide()

    def quit_button_clicked(self, widget, data=None):
        self.quit()

    def next_button_clicked(self, widget):
        if self.current_transaction.next == 0:
            self.msg_dialog("No more transactions")
        else:
            self.display_transaction(self.current_transaction.next)
            if self.in_transaction_mode():
                self.upper_treeview.get_selection().unselect_all()

    def prev_button_clicked(self, widget):
        if self.current_transaction.prev == 0:
            self.msg_dialog("No more transactions")
        else:
            self.display_transaction(self.current_transaction.prev)
            if self.in_transaction_mode():
                self.upper_treeview.get_selection().unselect_all()

    def next_chain_button_clicked(self, widget, data=None):
        if self.current_transaction.nref == 0:
            widget.error_bell()
            return True
        self.display_transaction(self.current_transaction.nref)
        if self.in_transaction_mode():
            self.upper_treeview.get_selection().unselect_all()

    def prev_chain_button_clicked(self, widget, data=None):
        if self.current_transaction.pref == 0:
            widget.error_bell()
            return True
        self.display_transaction(self.current_transaction.pref)
        if self.in_transaction_mode():
            self.upper_treeview.get_selection().unselect_all()


if __name__ == '__main__':
    parser = OptionParser()
    parser.set_defaults(ui_file=DEFAULT_UI_FILE,
                        icon_file=DEFAULT_ICON_FILE,
                        timeout=DEFAULT_TIMEOUT,
                        debug=[])
    parser.add_option("--ui", dest="ui_file", action="store",
                      help="Specify UI file")
    parser.add_option("--icon", dest="icon_file", action="store",
                      help="Specify icon file")
    parser.add_option("--timeout", dest="timeout", action="store",
                      type="int", help="Default connection timeout")
    parser.add_option("--debug", dest="debug", action="append",
                      help="Specify (multiple) debug options")
    (options, args) = parser.parse_args()
    if options.timeout > 5:
        print >>sys.stderr, "A timeout value larger than 5 is not a good idea."
    try:
        dsc_wrapper = DiscussWrapper(options.timeout)
    except ValueError as e:
        dlg = Gtk.MessageDialog(None, 0, Gtk.MessageType.ERROR,
                                Gtk.ButtonsType.CLOSE,
                                "Error initializing discuss interface")
        dlg.set_title("Xdsc")
        dlg.format_secondary_text("\n".join(str(e).split(':', 1)))
        dlg.run()
        sys.exit(255)
    if len(args):
        parser.error("Program does not take any arguments.")
    logging.basicConfig()
    options.debug = [x.lower() for x in options.debug]
    if 'ui' in options.debug:
        uilogger.setLevel(logging.DEBUG)
    if 'discuss' in options.debug:
        dslogger.setLevel(logging.DEBUG)
    # Because PyGObject does stupid things with SIGINT
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    xdsc = Xdsc(options.ui_file, options.icon_file, dsc_wrapper)
    Gtk.main()
    sys.exit(0)
