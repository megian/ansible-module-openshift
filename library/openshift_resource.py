#!/usr/bin/python

import json
from StringIO import StringIO
import tempfile
import re
#import logging

DOCUMENTATION = '''
---
module: openshift_resource
short_description: Creates and patches OpenShift resources.
description:
  - Creates and patches OpenShift resources idempotently
  - based on template or strategic merge patch.
options:
  namespace:
    description:
    - The namespace in which to configure resources
    default: None
    required: true
    aliases: []
  template:
    description:
    - Path to template of resources to configure
    - Mutually exclusive with I(patch)
    required: false
    default: None
    aliases: []
  app_name:
    description:
    - Name of application resources when instantiating the template,
    - corresponds to the C(--name) option of C(oc new-app).
    - Only relevant when I(template) parameter is given.
    required: false
    default: None
    aliases: []
  arguments:
    description:
    - Arguments to use when instantiating the template.
    - Only relevant when I(template) parameter is given.
    required: false
    default: None
    aliases: []
  patch:
    description:
    - Strategic merge patch to apply
    - Mutually exclusive with I(template)
    required: false
    default: None
    aliases: []
author:
- "Daniel Tschan <tschan@puzzle.ch>"
extends_documentation_fragment: []
'''

EXAMPLES = '''
# TODO
'''

class ResourceModule:
  def __init__(self, module):
    self.module = module

    self.changed = False
    self.msg = []
    self.arguments = []

    for key in module.params:
      setattr(self, key, module.params[key])

  def exemption(self, kind, current, patch, path):
    if patch is None or isinstance(patch, (dict, list)) and not patch:
      return True
    elif kind == 'DeploymentConfig' and re.match('.spec.template.spec.containers\[[0-9]+\].image', path):
      image = patch.split(':')[0]
      return ("/" + image + "@") in current

    return False


  def patch_applied(self, kind, name, current, patch, path = ""):
    #logging.debug(path)
    if current is None:
      if not patch is None:
        self.msg.append(self.namespace + "::" + kind + "/" + name + "{" + path + "}(" + str(patch) + " != " + str(current) + ")")
        return False
    elif isinstance(patch, dict):
      for key, val in patch.iteritems():
        if not self.patch_applied(kind, name, current.get(key), val, path + "." + key):
          return False
    elif isinstance(patch, list):
      if not self.sublist(kind, name, current, patch, path):
        return False
    else:
      if current != patch and not self.exemption(kind, current, patch, path):
        self.msg.append(self.namespace + "::" + kind + "/" + name + "{" + path + "}(" + str(patch) + " != " + str(current) + ")")
        return False

    return True


  def equalList(self, kind, resource, current, patch, path):
    """Compare two lists recursively."""
    if len(current) != len(patch):
      self.msg.append(self.namespace + "::" + kind + "/" + resource + "{" + path + "}(length mismatch)")
      return False

    for i, val in enumerate(patch):
        if not self.patch_applied(kind, resource, current[i], val, path + "[" + str(i) + "]"):
          return False

    return True


  def sublist(self, kind, name, current, patch, path):
    if not current and not patch:
      return True
    elif not current:
      self.msg.append(self.namespace + "::" + kind + "/" + name + "{" + path + "}(new)")
      return False
    elif isinstance(current[0], dict) and 'name' in current[0]:
      for i, patchVal in enumerate(patch):
        elementName = patchVal.get('name')
        if elementName is None:  # Patch contains element without name attribute => fall back to plain list comparison.
          #logging.debug("Patch contains element without name attribute => fall back to plain list comparison.")
          return self.equalList(kind, name, current, patch, path)
        curVals = [curVal for curVal in current if curVal.get('name') == elementName]
        if len(curVals) == 0:
           self.msg.append(self.namespace + "::" + kind + "/" + name + "{" + path + '[' + str(len(current)) + ']' + "}(new)")
           return False
        elif len(curVals) == 1:
          if not self.patch_applied(kind, name, curVals[0], patchVal, path + '[' + str(i) + ']'):
            return False
        else:
          module.fail_json(msg="Patch contains multiple attributes with name '" + elementName + "' under path: " + path)

    return True


  def export_resource(self, kind, name = None, label = None):
    if label:
      name = '-l ' + label

    (rc, stdout, stderr) = self.module.run_command(['oc', 'export', '-n', self.namespace, kind + '/' + name, '-o', 'json'])

    if rc == 0:
      result = json.load(StringIO(stdout))
    else:
      result = {}

    return result


  def create_resource(self, kind, name, object):
    if not self.module.check_mode:
      file = tempfile.NamedTemporaryFile(prefix=kind + '_' + name, delete=True)
      json.dump(object, file)
      file.flush()
      (rc, stdout, stderr) = self.module.run_command(['oc', 'create', '-n', self.namespace, '-f', file.name], check_rc=True)
      file.close()


  def patch_resource(self, kind, name, patch):
    if not self.module.check_mode:
      (rc, stdout, stderr) = self.module.run_command(['oc', 'patch', '-n', self.namespace, kind + '/' + name, '-p', json.dumps(patch)], check_rc=True)


  def update_resource(self, object, path = ""):
    kind = object.get('kind')
    name = object.get('metadata', {}).get('name')
    #logging.debug("update_resource " + str(kind) + " " + str(name))
    if not kind:
      self.module.fail_json(msg=path + ".kind is undefined!")
    if not name:
      self.module.fail_json(msg=path + ".metadata.name is undefined!")

    current = self.export_resource(kind, name)

    if not current:
      self.changed = True
      self.msg.append(self.namespace + "::" + kind + "/" + name + "(new)")
      self.create_resource(kind, name, object)
    elif not self.patch_applied(kind, name, current, object):
      self.changed = True
      self.patch_resource(kind, name, object)

    return self.changed


  def process_template(self, template_name, arguments):
    if arguments:
      args = " -p " + " -p ".join("=".join(_) for _ in arguments.items())
      #logging.info(args)
    else:
      args = ""

    if self.app_name:
      args += ' --name=' + self.app_name

    (rc, stdout, stderr) = self.module.run_command('oc new-app -o json ' + template_name + args, check_rc=True)

    if stderr:
      self.module.fail_json(msg=stderr)

    return json.load(StringIO(stdout))


  def apply_template(self, template_name, arguments):
    template = self.process_template(template_name, arguments)

    for i, object in enumerate(template['items']):
      self.update_resource(object, ".items[" + str(i) + "]")


def main():
    #logging.basicConfig(filename='/var/log/lib_appuio/openshift_resource.log', level=logging.INFO)

    module = AnsibleModule(
        argument_spec=dict(
            namespace = dict(type='str'),
            template = dict(type='str'),
            app_name = dict(type='str'),
            arguments = dict(type='dict'),
            patch = dict(type='dict'),
        ),
        supports_check_mode=True
    )

    resource = ResourceModule(module)

    if resource.template:
      resource.apply_template(resource.template, resource.arguments)
    else:
      resource.update_resource(resource.patch)

    module.exit_json(changed=resource.changed, msg=" ".join(resource.msg))


from ansible.module_utils.basic import *
if __name__ == "__main__":
    main()

