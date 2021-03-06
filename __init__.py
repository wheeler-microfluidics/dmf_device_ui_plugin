from datetime import datetime
from subprocess import Popen, CREATE_NEW_PROCESS_GROUP
import io
import json
import logging
import os
import sys
import time

from flatland import Boolean, Form, Integer, String
from microdrop.plugin_helpers import (AppDataController, StepOptionsController,
                                      get_plugin_info, hub_execute)
from microdrop.plugin_manager import (IPlugin, Plugin, PluginGlobals,
                                      ScheduleRequest, emit_signal, implements)
from microdrop.app_context import (get_app, get_hub_uri, SCREEN_WIDTH,
                                   SCREEN_HEIGHT, SCREEN_TOP, TITLEBAR_HEIGHT)
from path_helpers import path
from pygtkhelpers.gthreads import gtk_threadsafe
from pygtkhelpers.utils import refresh_gui
from si_prefix import si_format
import gobject
import gtk
import pandas as pd
import psutil

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions

gtk.gdk.threads_init()


logger = logging.getLogger(__name__)


PluginGlobals.push_env('microdrop.managed')


def kill_process_tree(pid, including_parent=True):
    '''
    Cross-platform function to kill a parent process and all child processes.

    Based on from `subprocess: deleting child processes in Windows <https://stackoverflow.com/a/4229404/345236>`_

    Parameters
    ----------
    pid : int
        Process ID of parent process.
    including_parent : bool, optional
        If ``True``, also kill parent process.
    '''
    parent = psutil.Process(pid)
    children = parent.children(recursive=True)
    for child in children:
        child.kill()
    gone, still_alive = psutil.wait_procs(children, timeout=5)
    if including_parent:
        parent.kill()
        parent.wait(5)



class DmfDeviceUiPlugin(AppDataController, StepOptionsController, Plugin):
    """
    This class is automatically registered with the PluginManager.

    .. versionchanged:: 2.10
        Set default window size and position according to **screen size** *and*
        **window titlebar size**.  Also, force default window size if
        ``MICRODROP_FIRST_RUN`` environment variable is set to non-empty value.
    """
    implements(IPlugin)
    version = get_plugin_info(path(__file__).parent).version
    plugin_name = get_plugin_info(path(__file__).parent).plugin_name

    AppFields = Form.of(
        String.named('video_config').using(default='', optional=True,
                                           properties={'show_in_gui': False}),
        String.named('surface_alphas').using(default='', optional=True,
                                             properties={'show_in_gui':
                                                         False}),
        String.named('canvas_corners').using(default='', optional=True,
                                             properties={'show_in_gui':
                                                         False}),
        String.named('frame_corners').using(default='', optional=True,
                                            properties={'show_in_gui': False}),
        Integer.named('x').using(default=.5 * SCREEN_WIDTH,
                                 optional=True,
                                 properties={'show_in_gui': False}),
        Integer.named('y').using(default=SCREEN_TOP, optional=True,
                                 properties={'show_in_gui': False}),
        Integer.named('width').using(default=.5 * SCREEN_WIDTH,
                                     optional=True,
                                     properties={'show_in_gui': False}),
        Integer.named('height').using(default=SCREEN_HEIGHT - 1.5 *
                                      TITLEBAR_HEIGHT, optional=True,
                                      properties={'show_in_gui': False}))

    StepFields = Form.of(Boolean.named('video_enabled')
                         .using(default=True, optional=True,
                                properties={'title': 'Video'}))

    def __init__(self):
        self.name = self.plugin_name
        self.gui_process = None
        self.gui_heartbeat_id = None
        self._gui_enabled = False
        self.alive_timestamp = None

    def reset_gui(self):
        '''
        .. versionchanged:: 2.2.2
            Use :func:`pygtkhelpers.gthreads.gtk_threadsafe` decorator around
            function to wait for GUI process, rather than using
            :func:`gobject.idle_add`, to make intention clear.

        .. versionchanged:: 2.9
            Refresh list of registered commands once device UI process has
            started.  The list of registered commands is used to dynamically
            generate items in the device UI context menu.
        '''
        py_exe = sys.executable

        # Set allocation based on saved app values (i.e., remember window size
        # and position from last run).
        app_values = self.get_app_values()
        if os.environ.get('MICRODROP_FIRST_RUN'):
            # Use default options for window allocation.
            default_app_values = self.get_default_app_options()
            for k in ('x', 'y', 'width', 'height'):
                app_values[k] = default_app_values[k]

        allocation_args = ['-a', json.dumps(app_values)]

        app = get_app()
        if app.config.data.get('advanced_ui', False):
            debug_args = ['-d']
        else:
            debug_args = []

        self.gui_process = Popen([py_exe, '-m',
                                  'dmf_device_ui.bin.device_view', '-n',
                                  self.name] + allocation_args + debug_args +
                                 ['fixed', get_hub_uri()],
                                 creationflags=CREATE_NEW_PROCESS_GROUP)
        self._gui_enabled = True

        def keep_alive():
            if not self._gui_enabled:
                self.alive_timestamp = None
                return False
            elif self.gui_process.poll() == 0:
                # GUI process has exited.  Restart.
                self.cleanup()
                self.reset_gui()
                return False
            else:
                self.alive_timestamp = datetime.now()
                # Keep checking.
                return True

        self.step_video_settings = None

        @gtk_threadsafe
        def _wait_for_gui():
            self.wait_for_gui_process()
            # Get current video settings from UI.
            app_values = self.get_app_values()
            # Convert JSON settings to 0MQ plugin API Python types.
            ui_settings = self.json_settings_as_python(app_values)
            self.set_ui_settings(ui_settings, default_corners=True)
            self.gui_heartbeat_id = gobject.timeout_add(1000, keep_alive)
            # Refresh list of electrode and route commands.
            hub_execute('microdrop.command_plugin', 'get_commands')

        # Call as thread-safe function, since function uses GTK.
        _wait_for_gui()

    def cleanup(self):
        '''
        .. versionchanged:: 2.2.2
            Catch any exception encountered during GUI process termination.

        .. versionchanged:: 2.3.1
            Use :func:`kill_process_tree` to terminate DMF device UI process.

            This ensures any child processes of the UI process (e.g., video
            input process) are also killed.

            See also:
            https://stackoverflow.com/a/44648162/345236

        .. versionchanged:: 2.7
            Only try to terminate the GUI process if it is still running.
        '''
        logger.info('Stop DMF device UI keep-alive timer')
        if self.gui_heartbeat_id is not None:
            # Stop keep-alive polling of device UI process.
            gobject.source_remove(self.gui_heartbeat_id)
        if self.gui_process is not None and self.gui_process.poll() is None:
            logger.info('Terminate DMF device UI process')
            try:
                kill_process_tree(self.gui_process.pid)
                logger.info('Close DMF device UI process `%s`',
                            self.gui_process.pid)
            except Exception:
                logger.info('Unexpected error closing DMF device UI process '
                            '`%s`', self.gui_process.pid, exc_info=True)
        else:
            logger.info('No active DMF device UI process')
        self.alive_timestamp = None

    def wait_for_gui_process(self, retry_count=20, retry_duration_s=1):
        '''
        .. versionchanged:: 2.7.2
            Do not execute `refresh_gui()` while waiting for response from
            `hub_execute()`.
        '''
        start = datetime.now()
        for i in xrange(retry_count):
            try:
                hub_execute(self.name, 'ping', timeout_s=5, silent=True)
            except Exception:
                logger.debug('[wait_for_gui_process] failed (%d of %d)', i + 1,
                             retry_count, exc_info=True)
            else:
                logger.info('[wait_for_gui_process] success (%d of %d)', i + 1,
                            retry_count)
                self.alive_timestamp = datetime.now()
                return
            for j in xrange(10):
                time.sleep(retry_duration_s / 10.)
                refresh_gui()
        raise IOError('Timed out after %ss waiting for GUI process to connect '
                      'to hub.' % si_format((datetime.now() -
                                             start).total_seconds()))

    def get_schedule_requests(self, function_name):
        """
        Returns a list of scheduling requests (i.e., ScheduleRequest instances)
        for the function specified by function_name.

        .. versionchanged:: 2.3.3
            Do not submit ``on_app_exit`` schedule request.  This is no longer
            necessary since ``hub_execute`` listening socket is no longer
            closed by ``microdrop.device_info_plugin`` during ``on_app_exit``
            callback.

        .. versionadded:: 2.9
            Enable _after_ command plugin and zmq hub plugin.
        """
        if function_name == 'on_plugin_enable':
            return [ScheduleRequest(p, self.name)
                    for p in ('microdrop.zmq_hub_plugin',
                              'microdrop.command_plugin',
                              'droplet_planning_plugin')]
        return []

    def on_app_exit(self):
        logger.info('Get current video settings from DMF device UI plugin.')
        json_settings = self.get_ui_json_settings()
        self.save_ui_settings(json_settings)
        self._gui_enabled = False
        self.cleanup()

    # #########################################################################
    # # DMF device UI 0MQ plugin settings
    def get_ui_json_settings(self):
        '''
        Get current video settings from DMF device UI plugin.

        Returns
        -------

            (dict) : DMF device UI plugin settings in JSON-compatible format
                (i.e., only basic Python data types).


        .. versionchanged:: 2.7.2
            Do not execute `refresh_gui()` while waiting for response from
            `hub_execute()`.
        '''
        video_settings = {}

        # Try to request video configuration.
        try:
            video_config = hub_execute(self.name, 'get_video_config',
                                       timeout_s=2)
        except IOError:
            logger.warning('Timed out waiting for device window size and '
                           'position request.')
        else:
            if video_config is not None:
                video_settings['video_config'] = video_config.to_json()
            else:
                video_settings['video_config'] = ''

        # Try to request allocation to save in app options.
        try:
            data = hub_execute(self.name, 'get_corners', timeout_s=2)
        except IOError:
            logger.warning('Timed out waiting for device window size and '
                           'position request.')
        else:
            if data:
                # Get window allocation settings (i.e., width, height, x, y).

                # Replace `df_..._corners` with CSV string named `..._corners`
                # (no `df_` prefix).
                for k in ('df_canvas_corners', 'df_frame_corners'):
                    if k in data:
                        data['allocation'][k[3:]] = data.pop(k).to_csv()
                video_settings.update(data['allocation'])

        # Try to request surface alphas.
        try:
            surface_alphas = hub_execute(self.name, 'get_surface_alphas',
                                         timeout_s=2)
        except IOError:
            logger.warning('Timed out waiting for surface alphas.')
        else:
            if surface_alphas is not None:
                video_settings['surface_alphas'] = surface_alphas.to_json()
            else:
                video_settings['surface_alphas'] = ''
        return video_settings

    def get_ui_settings(self):
        '''
        Get current video settings from DMF device UI plugin.

        Returns
        -------

            (dict) : DMF device UI plugin settings in Python types expected by
                DMF device UI plugin 0MQ commands.
        '''
        json_settings = self.get_ui_json_settings()
        return self.json_settings_as_python(json_settings)

    def json_settings_as_python(self, json_settings):
        '''
        Convert DMF device UI plugin settings from json format to Python types.

        Python types are expected by DMF device UI plugin 0MQ command API.

        Args
        ----

            json_settings (dict) : DMF device UI plugin settings in
                JSON-compatible format (i.e., only basic Python data types).

        Returns
        -------

            (dict) : DMF device UI plugin settings in Python types expected by
                DMF device UI plugin 0MQ commands.
        '''
        py_settings = {}

        corners = dict([(k, json_settings.get(k))
                        for k in ('canvas_corners', 'frame_corners')])

        if all(corners.values()):
            # Convert CSV corners lists for canvas and frame to
            # `pandas.DataFrame` instances
            for k, v in corners.iteritems():
                # Prepend `'df_'` to key to indicate the type as a data frame.
                py_settings['df_' + k] = pd.read_csv(io.BytesIO(bytes(v)),
                                                     index_col=0)

        for k in ('video_config', 'surface_alphas'):
            if k in json_settings:
                if not json_settings[k]:
                    py_settings[k] = pd.Series(None)
                else:
                    py_settings[k] = pd.Series(json.loads(json_settings[k]))

        return py_settings

    def save_ui_settings(self, video_settings):
        '''
        Save specified DMF device UI 0MQ plugin settings to persistent
        Microdrop configuration (i.e., settings to be applied when Microdrop is
        launched).

        Args
        ----

            video_settings (dict) : DMF device UI plugin settings in
                JSON-compatible format returned by `get_ui_json_settings`
                method (i.e., only basic Python data types).
        '''
        app_values = self.get_app_values()
        # Select subset of app values that are present in `video_settings`.
        app_video_values = dict([(k, v) for k, v in app_values.iteritems()
                                 if k in video_settings.keys()])

        # If the specified video settings differ from app values, update
        # app values.
        if app_video_values != video_settings:
            app_values.update(video_settings)
            self.set_app_values(app_values)

    def set_ui_settings(self, ui_settings, default_corners=False):
        '''
        Set DMF device UI settings from settings dictionary.

        Args
        ----

            ui_settings (dict) : DMF device UI plugin settings in format
                returned by `json_settings_as_python` method.


        .. versionchanged:: 2.7.2
            Do not execute `refresh_gui()` while waiting for response from
            `hub_execute()`.
        '''
        if self.alive_timestamp is None or self.gui_process is None:
            # Repeat until GUI process has started.
            raise IOError('GUI process not ready.')

        if 'video_config' in ui_settings:
            hub_execute(self.name, 'set_video_config',
                        video_config=ui_settings['video_config'], timeout_s=5)

        if 'surface_alphas' in ui_settings:
            hub_execute(self.name, 'set_surface_alphas',
                        surface_alphas=ui_settings['surface_alphas'],
                        timeout_s=5)

        if all((k in ui_settings) for k in ('df_canvas_corners',
                                            'df_frame_corners')):
            if default_corners:
                hub_execute(self.name, 'set_default_corners',
                            canvas=ui_settings['df_canvas_corners'],
                            frame=ui_settings['df_frame_corners'], timeout_s=5)
            else:
                hub_execute(self.name, 'set_corners',
                            df_canvas_corners=ui_settings['df_canvas_corners'],
                            df_frame_corners=ui_settings['df_frame_corners'],
                            timeout_s=5)

    # #########################################################################
    # # Plugin signal handlers
    def on_plugin_disable(self):
        self._gui_enabled = False
        self.cleanup()

    def on_plugin_enable(self):
        super(DmfDeviceUiPlugin, self).on_plugin_enable()
        self.reset_gui()

    def on_step_run(self):
        '''
        Handler called whenever a step is executed.

        Plugins that handle this signal must emit the on_step_complete signal
        once they have completed the step. The protocol controller will wait
        until all plugins have completed the current step before proceeding.

        .. versionchanged:: 2.2.2
            Emit ``on_step_complete`` signal within thread-safe function, since
            signal callbacks may use GTK.
        '''
        app = get_app()

        if (app.realtime_mode or app.running) and self.gui_process is not None:
            step_options = self.get_step_options()
            if not step_options['video_enabled']:
                command = 'disable_video'
            else:
                command = 'enable_video'

            hub_execute(self.name, command)

            # Call as thread-safe function, since signal callbacks may use GTK.
            gtk_threadsafe(emit_signal)('on_step_complete', [self.name, None])


PluginGlobals.pop_env()
