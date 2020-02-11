from . import exceptions
from maintenance import models
from logical.validators import (
    check_is_database_enabled,
    check_is_database_dead
)
from logical.errors import DisabledDatabase
from system.models import Configuration
from notification.tasks import TaskRegister


class AddReadOnlyInstanceService:

    def __init__(self, request, database, retry=False):
        self.request = request
        self.database = database
        self.manager = None
        self.number_of_instances = 0
        self.number_of_instances_before = 0
        self.retry = retry
        self.task_params = {}

        self.initialize()

    def initialize(self):
        self.load_manager()

    def load_manager(self):
        if self.retry:
            self.manager = models.AddInstancesToDatabase.objects.filter(
                database=self.database
            ).last()
            self.number_of_instances = self.manager.number_of_instances

            if not self.manager:
                error = "Database does not have add_database_instances"
                raise exceptions.ManagerNotFound(error)
            elif not self.manager.is_status_error:
                error = ("Cannot do retry, last add_instances_to_database. "
                         "Status is '{}'!").format(
                            self.manager.get_status_display())
                raise exceptions.ManagerInvalidStatus(error)

    def load_number_of_instances(self):
        if self.retry:
            self.number_of_instances = self.manager.number_of_instances
            self.number_of_instances_before = (
                self.manager.number_of_instances_before
            )
        else:
            if 'add_read_qtd' in self.request.POST:
                self.number_of_instances = int(
                    self.request.POST['add_read_qtd']
                )
            self.number_of_instances_before = (
                self.database.infra.last_vm_created
            )

    def check_database_status(self):
        try:
            check_is_database_dead(self.database.id, 'Add read-only instances')
            check_is_database_enabled(
                self.database.id,
                'Add read-only instances',
                ['notification.tasks.add_instances_to_database']
            )
            return (True, '')
        except DisabledDatabase as err:
            return (False, err.message)

    def is_ha(self):
        if not self.database.plan.replication_topology.has_horizontal_scalability:
            return False
        return True

    def execute(self):
        self.load_number_of_instances()

        if not self.number_of_instances:
            raise exceptions.RequiredNumberOfInstances(
                'Number of instances is required'
            )

        status, message = self.check_database_status()
        if not status:
            raise exceptions.DatabaseNotAvailable(message)

        if not self.is_ha():
            raise exceptions.DatabaseIsNotHA(
                'Database topology do not have horizontal scalability'
            )

        max_read_hosts = Configuration.get_by_name_as_int('max_read_hosts', 5)
        qtd_new_hosts = self.number_of_instances
        current_read_nodes = len(self.database.infra.instances.filter(read_only=True))
        total_read_hosts = qtd_new_hosts + current_read_nodes
        if total_read_hosts > max_read_hosts:
            raise exceptions.ReadOnlyHostsLimit(
                ('Current limit of read only hosts is {} and you are trying '
                 'to setup {}').format(
                    max_read_hosts, total_read_hosts
                )
            )

        self.task_params = dict(
            database=self.database,
            user=self.request.user,
            number_of_instances=qtd_new_hosts,
            number_of_instances_before_task=self.number_of_instances_before
        )

        if self.retry:
            since_step = self.manager.current_step
            self.task_params['since_step'] = since_step

        TaskRegister.database_add_instances(**self.task_params)
