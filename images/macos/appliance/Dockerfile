# Preserve the deployed image digest with --build-arg BASE=<existing image>.
# Compose MUST retain its existing command and disk mounts. Defining a new
# entrypoint resets an inherited CMD; the wrapper refuses an empty command.
ARG BASE=sickcodes/docker-osx:latest
FROM ${BASE}
# The shared installation media is mounted read-only by every appliance.
# A disposable QEMU overlay allows IDE writes without modifying the shared
# read-only base. RAM is configured in GiB by the controller.
RUN test "$(grep -c 'id=InstallMedia,if=none,file=' /home/arch/OSX-KVM/Launch.sh)" = 1 \
    && grep -qF '${RAM:-4}000' /home/arch/OSX-KVM/Launch.sh \
    && sed -i 's/id=InstallMedia,if=none,file=/id=InstallMedia,if=none,snapshot=on,file=/; s/${RAM:-4}000/$(( ${RAM:-4} * 1024 ))/' /home/arch/OSX-KVM/Launch.sh
ENV CPU=Haswell-noTSX EXTRA="-display none -vnc 0.0.0.0:99,password=off"
COPY --chmod=0755 startup /usr/local/bin/nomercy-appliance-start
LABEL nomercy.appliance_boot_cleanup="true"
ENTRYPOINT ["/usr/local/bin/nomercy-appliance-start"]
